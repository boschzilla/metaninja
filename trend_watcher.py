"""
PoE Price Trend Watcher

Fetches poe.ninja prices on demand (or on a schedule) and shows trends:
  - Top Movers  — biggest absolute % change in either direction
  - Risers      — biggest positive % change
  - Fallers     — biggest negative % change
  - Search      — look up any item and view its full price chart

Usage:
    python trend_watcher.py [--league LeagueName]

Requires matplotlib for the price-history chart:
    pip install matplotlib
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import Optional

import price_db
from fetch_ninja_prices import (
    fetch_url, parse_currency, parse_items,
    CATEGORIES, BASE_CURRENCY_URL, BASE_ITEM_URL, LEAGUES_URL,
)

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ── Theme ──────────────────────────────────────────────────────────────────────
BG      = "#1e1e1e"
BG2     = "#2a2a2a"
BG3     = "#333333"
FG      = "#d4d4d4"
FG_DIM  = "#707070"
GOLD    = "#FFD700"
GREEN   = "#90EE90"
RED     = "#FF6B6B"

FONT_MONO   = ("Consolas", 9)
FONT_MONO_B = ("Consolas", 9, "bold")
FONT_HDR    = ("Consolas", 11, "bold")

# ── Column definitions ─────────────────────────────────────────────────────────
MOVER_COLS = [
    ("name",        "Name",      260, "w"),
    ("category",    "Category",  110, "w"),
    ("first_price", "Old (c)",    75, "e"),
    ("last_price",  "Now (c)",    75, "e"),
    ("abs_change",  "Δ Chaos",    75, "e"),
    ("pct_change",  "Δ %",        65, "e"),
    ("snap_count",  "Snaps",      50, "center"),
]

SEARCH_COLS = [
    ("name",       "Name",     310, "w"),
    ("category",   "Category", 130, "w"),
    ("last_price", "Now (c)",   90, "e"),
]


# ── Formatting helpers ─────────────────────────────────────────────────────────
def _fc(v) -> str:
    return f"{v:.1f}c" if v is not None else "—"

def _fpct(v) -> str:
    return f"{v:+.1f}%" if v is not None else "—"

def _fabs(v) -> str:
    return f"{v:+.1f}c" if v is not None else "—"


# ── Background fetch ───────────────────────────────────────────────────────────
def do_fetch(league: str, on_progress, on_done) -> None:
    """
    Fetch all poe.ninja categories for *league* and store results in price_db.
    Calls on_progress(msg) as each category is fetched, then on_done(errors).
    Intended to run in a daemon thread.
    """
    now = datetime.utcnow().isoformat()
    errors: list[str] = []

    for (cat_type, endpoint_kind, kind) in CATEGORIES:
        on_progress(f"Fetching {cat_type}…")
        url = (
            BASE_CURRENCY_URL.format(league=league, type=cat_type)
            if endpoint_kind == "currency"
            else BASE_ITEM_URL.format(league=league, type=cat_type)
        )
        try:
            data = fetch_url(url)
            items = (
                parse_currency(data, cat_type)
                if kind == "currency"
                else parse_items(data, cat_type)
            )
            price_db.insert_snapshot(cat_type, items, now)
        except Exception as exc:
            errors.append(f"{cat_type}: {exc}")
        time.sleep(0.4)     # be polite to the API

    on_done(errors)


# ── GUI ────────────────────────────────────────────────────────────────────────
class TrendApp:
    def __init__(self, root: tk.Tk, league: str) -> None:
        self.root = root
        self.league = league
        self._fetch_thread: Optional[threading.Thread] = None
        self._auto_job = None
        self._sort_state: dict[tuple, bool] = {}   # (tree_id, col) → reverse

        # Per-tree filter state
        self._tree_data:        dict[int, list]       = {}
        self._filter_name:      dict[int, tk.StringVar] = {}
        self._filter_cat:       dict[int, tk.StringVar] = {}
        self._filter_count:     dict[int, tk.StringVar] = {}
        self._filter_cat_menu:  dict[int, ttk.Combobox] = {}

        price_db.init_db()
        self._build_ui()
        self._refresh_tables()
        self._fetch_leagues_async()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = self.root
        root.title("PoE Price Trend Watcher")
        root.configure(bg=BG)
        root.minsize(820, 640)
        self._apply_styles()

        # Top bar
        top = tk.Frame(root, bg="#111111", pady=5)
        top.pack(fill="x")
        self._build_top_bar(top)

        # DB info bar
        info = tk.Frame(root, bg=BG2, pady=3)
        info.pack(fill="x")
        self._db_info_var = tk.StringVar(value="")
        tk.Label(
            info, textvariable=self._db_info_var,
            bg=BG2, fg=FG_DIM, font=FONT_MONO, anchor="w",
        ).pack(side="left", padx=8)

        # Vertical pane: notebook (top) + chart (bottom)
        pane = tk.PanedWindow(
            root, orient="vertical", bg=BG,
            sashwidth=5, sashrelief="flat", sashpad=2,
        )
        pane.pack(fill="both", expand=True, padx=6, pady=(4, 6))

        nb_frame = tk.Frame(pane, bg=BG)
        pane.add(nb_frame, minsize=200, stretch="always")

        nb = ttk.Notebook(nb_frame)
        nb.pack(fill="both", expand=True)
        self._movers_tree  = self._make_mover_tab(nb, "↑↓ Movers")
        self._risers_tree  = self._make_mover_tab(nb, "↑ Risers")
        self._fallers_tree = self._make_mover_tab(nb, "↓ Fallers")
        self._build_search_tab(nb)

        chart_frame = tk.Frame(pane, bg=BG)
        pane.add(chart_frame, minsize=160, stretch="never")
        self._build_chart_panel(chart_frame)

    def _apply_styles(self) -> None:
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("TNotebook",       background=BG,  borderwidth=0)
        s.configure("TNotebook.Tab",   background=BG3, foreground=FG,
                    font=FONT_MONO_B, padding=[8, 4])
        s.map("TNotebook.Tab",         background=[("selected", BG2)])
        s.configure("Treeview",
                    background=BG2, foreground=FG, fieldbackground=BG2,
                    rowheight=22, font=FONT_MONO, borderwidth=0)
        s.configure("Treeview.Heading", background=BG3, foreground=FG,
                    font=FONT_MONO_B)
        s.map("Treeview",              background=[("selected", "#3a5a8a")])
        s.configure("TPanedwindow",    background=BG)
        s.configure("Vertical.TScrollbar",
                    background=BG3, troughcolor=BG2,
                    borderwidth=0, arrowcolor=FG)
        s.configure("TCombobox",
                    fieldbackground=BG3, background=BG3,
                    foreground=FG, arrowcolor=FG,
                    selectbackground=BG3, selectforeground=FG)
        s.map("TCombobox",
              fieldbackground=[("readonly", BG3)],
              foreground=[("readonly", FG)])

    def _build_top_bar(self, top: tk.Frame) -> None:
        tk.Label(
            top, text="PoE Price Trend Watcher",
            bg="#111111", fg=FG, font=FONT_HDR,
        ).pack(side="left", padx=10)

        tk.Label(top, text="League:", bg="#111111", fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(20, 4))
        self._league_var = tk.StringVar(value=self.league)
        self._league_combo = ttk.Combobox(
            top, textvariable=self._league_var, width=14,
            font=FONT_MONO, values=[self.league],
        )
        self._league_combo.pack(side="left")
        tk.Button(
            top, text="↺", bg=BG3, fg=FG_DIM,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=self._fetch_leagues_async,
        ).pack(side="left", padx=(2, 0))

        self._fetch_btn = tk.Button(
            top, text="Fetch Now", bg="#3a6a3a", fg=FG,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=self._fetch_now,
        )
        self._fetch_btn.pack(side="left", padx=(12, 0))

        # Auto-fetch toggle
        self._auto_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            top, text="Auto", variable=self._auto_var,
            bg="#111111", fg=FG, selectcolor=BG3,
            activebackground="#111111", activeforeground=FG,
            font=FONT_MONO, command=self._on_auto_toggle,
        ).pack(side="left", padx=(14, 2))
        self._interval_var = tk.IntVar(value=1)
        tk.Spinbox(
            top, from_=1, to=24, textvariable=self._interval_var,
            width=3, bg=BG3, fg=FG, font=FONT_MONO,
            relief="flat", buttonbackground=BG3,
        ).pack(side="left")
        tk.Label(top, text="h", bg="#111111", fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(2, 0))

        tk.Button(
            top, text="↺ Refresh", bg=BG3, fg=FG,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=self._refresh_tables,
        ).pack(side="left", padx=(14, 0))

        tk.Button(
            top, text="⟳ Reload", bg="#4a3a6a", fg=FG,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=self._reload,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            top, text="🗑 Delete History", bg="#6a2a2a", fg=FG,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=self._delete_history,
        ).pack(side="left", padx=(8, 0))

        self._status_var = tk.StringVar(value="Ready")
        tk.Label(
            top, textvariable=self._status_var,
            bg="#111111", fg=FG_DIM, font=FONT_MONO,
        ).pack(side="right", padx=10)

    def _make_mover_tab(self, nb: ttk.Notebook, title: str) -> ttk.Treeview:
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text=title)

        # ── Filter bar ────────────────────────────────────────────────────
        fbar = tk.Frame(frame, bg=BG2, pady=5)
        fbar.pack(fill="x")

        tk.Label(fbar, text="Filter:", bg=BG2, fg=FG,
                 font=FONT_MONO).pack(side="left", padx=(8, 4))

        name_var = tk.StringVar()
        name_entry = tk.Entry(
            fbar, textvariable=name_var, width=24,
            bg=BG3, fg=FG, insertbackground=FG,
            font=FONT_MONO, relief="flat",
        )
        name_entry.pack(side="left", padx=(0, 4))

        tk.Button(
            fbar, text="✕", bg=BG3, fg=FG_DIM,
            font=FONT_MONO, relief="flat", cursor="hand2",
            command=lambda v=name_var: v.set(""),
        ).pack(side="left", padx=(0, 10))

        tk.Label(fbar, text="Category:", bg=BG2, fg=FG_DIM,
                 font=FONT_MONO).pack(side="left", padx=(0, 4))
        cat_var = tk.StringVar(value="All")
        cat_menu = ttk.Combobox(
            fbar, textvariable=cat_var, width=18,
            font=FONT_MONO, state="readonly",
        )
        cat_menu["values"] = ["All"]
        cat_menu.pack(side="left")

        count_var = tk.StringVar(value="")
        tk.Label(fbar, textvariable=count_var,
                 bg=BG2, fg=FG_DIM, font=FONT_MONO).pack(side="right", padx=8)

        # ── Treeview ──────────────────────────────────────────────────────
        tree = ttk.Treeview(
            frame, columns=[c[0] for c in MOVER_COLS],
            show="headings", selectmode="browse",
        )
        for col_id, label, width, anchor in MOVER_COLS:
            tree.heading(
                col_id, text=label,
                command=lambda c=col_id, t=tree: self._sort_tree(t, c),
            )
            tree.column(col_id, width=width, minwidth=40, anchor=anchor)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)

        tree.tag_configure("rise", foreground=GREEN)
        tree.tag_configure("fall", foreground=RED)
        tree.tag_configure("flat", foreground=FG_DIM)
        tree.bind("<<TreeviewSelect>>", lambda e, t=tree: self._on_select(t))

        # Register filter state for this tree
        tid = id(tree)
        self._tree_data[tid]       = []
        self._filter_name[tid]     = name_var
        self._filter_cat[tid]      = cat_var
        self._filter_count[tid]    = count_var
        self._filter_cat_menu[tid] = cat_menu

        name_var.trace_add("write", lambda *_: self._apply_filter(tree))
        cat_var.trace_add("write",  lambda *_: self._apply_filter(tree))

        return tree

    def _build_search_tab(self, nb: ttk.Notebook) -> None:
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text="Search")

        bar = tk.Frame(frame, bg=BG2, pady=6)
        bar.pack(fill="x")
        tk.Label(bar, text="Search:", bg=BG2, fg=FG,
                 font=FONT_MONO).pack(side="left", padx=8)
        self._search_var = tk.StringVar()
        ent = tk.Entry(
            bar, textvariable=self._search_var, width=32,
            bg=BG3, fg=FG, insertbackground=FG,
            font=FONT_MONO, relief="flat",
        )
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda _: self._do_search())
        tk.Button(
            bar, text="Go", bg=BG3, fg=FG, font=FONT_MONO,
            relief="flat", cursor="hand2", command=self._do_search,
        ).pack(side="left", padx=4)

        tree = ttk.Treeview(
            frame, columns=[c[0] for c in SEARCH_COLS],
            show="headings", selectmode="browse",
        )
        for col_id, label, width, anchor in SEARCH_COLS:
            tree.heading(col_id, text=label)
            tree.column(col_id, width=width, minwidth=40, anchor=anchor)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)

        tree.bind("<<TreeviewSelect>>", lambda e: self._on_select(tree))
        self._search_tree = tree

    def _build_chart_panel(self, frame: tk.Frame) -> None:
        self._item_label_var = tk.StringVar(value="Select an item to view its price history")
        tk.Label(
            frame, textvariable=self._item_label_var,
            bg=BG, fg=FG_DIM, font=FONT_MONO, anchor="w",
        ).pack(fill="x", padx=4, pady=(2, 0))

        if HAS_MPL:
            self._fig = Figure(figsize=(10, 2.0), dpi=96, facecolor=BG)
            self._ax  = self._fig.add_subplot(111, facecolor=BG2)
            self._fig.subplots_adjust(left=0.07, right=0.99, top=0.80, bottom=0.32)
            self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=frame)
            self._mpl_canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            tk.Label(
                frame,
                text="Install matplotlib to enable price charts:  pip install matplotlib",
                bg=BG, fg="#cc8844", font=FONT_MONO,
            ).pack(expand=True)

    # ── Table population ───────────────────────────────────────────────────

    def _refresh_tables(self) -> None:
        self._status_var.set("Refreshing…")
        self.root.update_idletasks()
        self._update_db_info()
        self._fill_mover_tree(self._movers_tree,  price_db.get_movers())
        self._fill_mover_tree(self._risers_tree,  price_db.get_risers())
        self._fill_mover_tree(self._fallers_tree, price_db.get_fallers())
        self._status_var.set(f"Refreshed — {datetime.now().strftime('%H:%M:%S')}")

    def _update_db_info(self) -> None:
        snaps = price_db.snapshot_count()
        times = price_db.get_snapshot_times()
        if times:
            last = times[0][:16].replace("T", " ")
            self._db_info_var.set(
                f"{snaps} snapshot(s) in database  •  last fetch: {last} UTC"
            )
        else:
            self._db_info_var.set("No data yet — click Fetch Now to start collecting prices")

    def _fill_mover_tree(self, tree: ttk.Treeview, rows: list) -> None:
        tid = id(tree)
        self._tree_data[tid] = rows

        # Refresh category dropdown with values present in this data set
        cats = sorted({r["category"] for r in rows if r.get("category")})
        menu = self._filter_cat_menu[tid]
        menu["values"] = ["All"] + cats
        if self._filter_cat[tid].get() not in ["All"] + cats:
            self._filter_cat[tid].set("All")

        self._apply_filter(tree)

    def _apply_filter(self, tree: ttk.Treeview) -> None:
        tid      = id(tree)
        rows     = self._tree_data.get(tid, [])
        name_q   = self._filter_name[tid].get().lower().strip()
        cat_q    = self._filter_cat[tid].get()

        filtered = [
            r for r in rows
            if (not name_q or name_q in r["name"].lower())
            and (cat_q == "All" or r.get("category") == cat_q)
        ]

        tree.delete(*tree.get_children())
        for r in filtered:
            pct = r.get("pct_change") or 0
            tag = "rise" if pct > 0 else ("fall" if pct < 0 else "flat")
            tree.insert("", "end", values=(
                r["name"],
                r["category"],
                _fc(r.get("first_price")),
                _fc(r.get("last_price")),
                _fabs(r.get("abs_change")),
                _fpct(pct),
                r.get("snap_count", ""),
            ), tags=(tag,))

        total = len(rows)
        shown = len(filtered)
        self._filter_count[tid].set(
            f"{shown} of {total}" if shown != total else f"{total} items"
        )

    def _do_search(self) -> None:
        q = self._search_var.get().strip()
        if not q:
            return
        tree = self._search_tree
        tree.delete(*tree.get_children())
        for r in price_db.search_items(q):
            tree.insert("", "end", values=(
                r["name"],
                r["category"],
                _fc(r.get("last_price")),
            ))

    def _sort_tree(self, tree: ttk.Treeview, col: str) -> None:
        """Toggle-sort a treeview by column (click once = asc, again = desc)."""
        key = (id(tree), col)
        reverse = self._sort_state.get(key, False)

        def sort_key(iid: str):
            v = tree.set(iid, col)
            try:
                return float(v.replace("c", "").replace("%", "")
                              .replace("+", "").replace("—", "0").replace(" ", ""))
            except ValueError:
                return v.lower()

        items = sorted(tree.get_children(), key=sort_key, reverse=reverse)
        for idx, iid in enumerate(items):
            tree.move(iid, "", idx)

        self._sort_state[key] = not reverse
        arrow = " ▼" if reverse else " ▲"
        # Update heading to show sort direction
        for col_id, label, _, _ in MOVER_COLS:
            current = tree.heading(col_id, "text")
            clean = current.rstrip(" ▲▼")
            tree.heading(col_id, text=clean + (arrow if col_id == col else ""))

    # ── Chart ──────────────────────────────────────────────────────────────

    def _on_select(self, tree: ttk.Treeview) -> None:
        sel = tree.selection()
        if not sel:
            return
        name = tree.item(sel[0], "values")[0]
        self._item_label_var.set(f"Price history: {name}")
        if HAS_MPL:
            self._update_chart(name)

    def _update_chart(self, name: str) -> None:
        history = price_db.get_history(name)
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG2)
        for spine in ax.spines.values():
            spine.set_color("#555555")
        ax.tick_params(colors=FG_DIM, labelsize=7)

        if not history:
            ax.set_title("No history found", color=FG_DIM, fontsize=8)
            self._mpl_canvas.draw()
            return

        times  = [datetime.fromisoformat(r["fetched_at"]) for r in history]
        prices = [r["chaos_value"] for r in history]

        ax.plot(times, prices, color=GOLD, linewidth=1.5, marker="o", markersize=3)
        ax.fill_between(times, prices, alpha=0.10, color=GOLD)

        if len(prices) > 1 and prices[0] > 0:
            pct = (prices[-1] - prices[0]) * 100.0 / prices[0]
            tc    = GREEN if pct > 0 else (RED if pct < 0 else FG)
            title = (
                f"{name}  •  {prices[-1]:.1f}c  "
                f"({pct:+.1f}%  over {len(prices)} snapshots)"
            )
        else:
            tc    = FG
            title = f"{name}  •  {prices[-1]:.1f}c"

        ax.set_title(title, color=tc, fontsize=8, pad=3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
        self._fig.autofmt_xdate(rotation=25, ha="right")
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
        ax.set_ylabel("chaos", color=FG_DIM, fontsize=7, labelpad=3)
        ax.grid(True, color="#3a3a3a", linewidth=0.5)
        self._mpl_canvas.draw()

    # ── Fetch control ──────────────────────────────────────────────────────

    def _fetch_now(self) -> None:
        if self._fetch_thread and self._fetch_thread.is_alive():
            return
        league = self._league_var.get().strip() or self.league
        self._fetch_btn.config(state="disabled")
        self._status_var.set("Starting fetch…")

        def on_progress(msg: str) -> None:
            self.root.after(0, lambda m=msg: self._status_var.set(m))

        def on_done(errors: list) -> None:
            def _finish() -> None:
                self._fetch_btn.config(state="normal")
                self._refresh_tables()
                # Set final message after refresh so it isn't overwritten by "Refreshed"
                if errors:
                    self._status_var.set(
                        f"Fetch done — {len(errors)} error(s) — {datetime.now().strftime('%H:%M:%S')}"
                    )
                else:
                    self._status_var.set(
                        f"Fetch complete — {datetime.now().strftime('%H:%M:%S')}"
                    )
            self.root.after(0, _finish)

        self._fetch_thread = threading.Thread(
            target=do_fetch, args=(league, on_progress, on_done), daemon=True,
        )
        self._fetch_thread.start()

    # ── Auto-fetch ──────────────────────────────────────────────────────────

    def _on_auto_toggle(self) -> None:
        if self._auto_var.get():
            self._schedule_auto()
        elif self._auto_job:
            self.root.after_cancel(self._auto_job)
            self._auto_job = None

    def _schedule_auto(self) -> None:
        ms = max(1, self._interval_var.get()) * 3_600_000
        self._auto_job = self.root.after(ms, self._auto_tick)

    def _auto_tick(self) -> None:
        self._fetch_now()
        if self._auto_var.get():
            self._schedule_auto()

    # ── League loader ──────────────────────────────────────────────────────

    def _fetch_leagues_async(self) -> None:
        self._status_var.set("Loading leagues…")

        def _run() -> None:
            try:
                data    = fetch_url(LEAGUES_URL)
                leagues = [e["id"] for e in data]
                self.root.after(0, lambda l=leagues: self._on_leagues_loaded(l))
            except Exception as exc:
                self.root.after(0, lambda: self._status_var.set(f"League load failed: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_leagues_loaded(self, leagues: list) -> None:
        self._league_combo["values"] = leagues
        current = self._league_var.get()
        if current not in leagues and leagues:
            self._league_var.set(leagues[0])
        self._status_var.set("Ready")

    # ── Delete history ─────────────────────────────────────────────────────

    def _delete_history(self) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Delete History",
            "Delete ALL price history from the database?\nThis cannot be undone.",
            icon="warning",
        ):
            return
        n = price_db.delete_all_history()
        self._status_var.set(f"Deleted {n} row(s)")
        self._refresh_tables()

    # ── Reload ─────────────────────────────────────────────────────────────

    def _reload(self) -> None:
        """Replace the current process with a fresh run of this script."""
        self.root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="PoE Price Trend Watcher")
    parser.add_argument("--league", "-l", default="Standard",
                        help="League name (default: Standard)")
    args = parser.parse_args()

    root = tk.Tk()
    TrendApp(root, args.league)
    root.mainloop()


if __name__ == "__main__":
    main()
