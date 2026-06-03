import sys
import os
import os.path as op
import threading
import queue
import time
import pandas as pd
import numpy as np
from datetime import datetime, date
import re
import math

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import dearpygui.dearpygui as dpg

dpg.create_context()

from DiscordAlertsTrader.configurator import cfg, channel_ids
from DiscordAlertsTrader.brokerages import get_brokerage
from DiscordAlertsTrader import gui_generator as gg
from DiscordAlertsTrader.discord_bot import DiscordBot
from DiscordAlertsTrader.message_parser import parse_trade_alert, ordersymb_to_str

# ─── Font ───────────────────────────────────────────────────────────────
try:
    with dpg.font_registry():
        dpg.bind_font(dpg.add_font(r"C:\Windows\Fonts\segoeui.ttf", 16))
except:
    pass

# ─── Theme ──────────────────────────────────────────────────────────────
with dpg.theme() as global_theme:
    with dpg.theme_component(dpg.mvAll):
        dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4, category=dpg.mvThemeCat_Core)
        dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 6, category=dpg.mvThemeCat_Core)
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 4, category=dpg.mvThemeCat_Core)
        dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 4, category=dpg.mvThemeCat_Core)
        dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6, category=dpg.mvThemeCat_Core)
        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4, category=dpg.mvThemeCat_Core)

dpg.bind_theme(global_theme)

# ─── Shared State ──────────────────────────────────────────────────────
bksession = None
discord_bot = None
trade_queue = queue.Queue(maxsize=100)
_refresh_count = 0
_msg_count = 0
_last_order_len = 0

# ─── Settings Helpers ──────────────────────────────────────────────────
_SETTINGS_EXCLUDE = {'root', 'col_names'}
_SENSITIVE_KEYS = {'discord_token', 'LOGIN_PWD', 'TRADING_PIN', 'SECURITY_DID'}

def _tag(section, key):
    return '_cfg_' + re.sub(r'[^a-zA-Z0-9_]', '_', section) + '_' + re.sub(r'[^a-zA-Z0-9_]', '_', key)

def _val_type(val):
    if val.lower() in ('true', 'false'):
        return 'bool'
    try:
        int(val); return 'int'
    except: pass
    try:
        float(val); return 'float'
    except: pass
    return 'str'

def _sensitive(section, key):
    return any(s.lower() in key.lower() for s in _SENSITIVE_KEYS)

def _get_config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

def fmt_pnl_color(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return (180, 180, 180)
    if val > 0:
        return (80, 220, 100)
    elif val < 0:
        return (240, 80, 80)
    return (180, 180, 180)

def safe_float(val, default=0.0):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    try:
        return float(val)
    except:
        return default

def clear_children(parent):
    for child in dpg.get_item_children(parent, 1):
        dpg.delete_item(child)

def add_message(text, color=(200, 200, 200)):
    global _msg_count
    _msg_count += 1
    if _msg_count > 500:
        clear_children("msg_content")
        _msg_count = 0
    with dpg.group(horizontal=True, parent="msg_content"):
        dpg.add_text(f"{datetime.now().strftime('%H:%M:%S')}  ", color=(120, 120, 120))
        dpg.add_text(text, color=color, wrap=700)
    try:
        dpg.set_y_scroll("msg_content", dpg.get_y_scroll_max("msg_content") + 50)
    except:
        pass

# ─── Dashboard ─────────────────────────────────────────────────────────
def build_dashboard():
    clear_children("dash_content")

    pf, _ = gg.get_portf_data({"Canceled": True, "Rejected": False, "live PnL": False,
                                "Closed": False, "Open": False, "NegPnL": False, "PosPnL": False,
                                "stocks": True, "options": False, "bto": False, "stc": False})

    open_positions = [r for r in pf if r and len(r) > 12 and str(r[12]) == "Yes"]
    wins = 0
    total_closed = 0

    for r in pf:
        if r and len(r) > 12:
            if str(r[12]) == "No":
                total_closed += 1
                if safe_float(r[12]) > 0:
                    wins += 1

    win_rate = (wins / total_closed * 100) if total_closed else 0

    acc_bal = "N/A"
    if bksession:
        try:
            info = gg.get_acc_bals(bksession)
            if info and len(info) > 1:
                acc = info[1]
                acc_bal = f"${float(acc['balance']):,.0f}"
        except:
            pass

    with dpg.group(horizontal=True, parent="dash_content"):
        dpg.add_text(f"Balance: {acc_bal}")
        dpg.add_text(f"    Open: {len(open_positions)}    Closed: {total_closed}    Win Rate: {win_rate:.0f}%")

    dpg.add_separator(parent="dash_content")
    dpg.add_text("Open Positions", parent="dash_content")

    if not open_positions:
        dpg.add_text("  No open positions", parent="dash_content", color=(150, 150, 150))
    else:
        for r in open_positions[:10]:
            if len(r) < 25:
                continue
            sym = str(r[14]) if r[14] else "?"
            pnl = safe_float(r[12])
            pnl_dol = safe_float(r[13])
            price = str(r[18]) if r[18] else "-"
            price_actual = str(r[19]) if r[19] else "-"
            qty = str(r[20]) if r[20] else "-"
            trader = str(r[15])[:12] if r[15] else "-"
            col = fmt_pnl_color(pnl)

            with dpg.group(horizontal=True, parent="dash_content"):
                with dpg.child_window(width=-1, height=54, menubar=False):
                    with dpg.group(horizontal=True):
                        dpg.add_text(f"{sym}", color=(220, 220, 255))
                        dpg.add_text(f"{pnl:+.1f}%", color=col)
                        dpg.add_text(f"${pnl_dol:+.0f}", color=col)
                        dpg.add_text(f"  Q:{qty}  E:{price}  M:{price_actual}  {trader}")

    dpg.add_separator(parent="dash_content")
    dpg.add_text("PnL History (last 30 trades)", parent="dash_content")

    if pf:
        pnl_vals = []
        for r in pf[-30:]:
            if r and len(r) > 12:
                pnl_vals.append(safe_float(r[12]))
        if pnl_vals:
            with dpg.plot(label="PnL History", height=200, width=-1, parent="dash_content"):
                x_ax = dpg.add_plot_axis(dpg.mvXAxis, label="Trade", no_tick_labels=True)
                y_ax = dpg.add_plot_axis(dpg.mvYAxis, label="PnL %")
                dpg.set_axis_limits(y_ax, min(pnl_vals) - 5, max(pnl_vals) + 5)
                dpg.add_bar_series(list(range(len(pnl_vals))), pnl_vals, parent=y_ax, weight=1)

# ─── Portfolio Table ───────────────────────────────────────────────────
def build_portfolio():
    clear_children("port_content")

    dt, hdr = gg.get_portf_data({"Canceled": False, "live PnL": False,
                                  "stocks": True, "options": True, "bto": False, "stc": False})

    skip_cols = {"Live", "S1-ordID", "S2-ordID", "S3-ordID",
                 "BTO-avg-Status", "S1-Status", "S2-Status", "S3-Status",
                 "S1-Qty", "S2-Qty", "S3-Qty", "filledQty", "N Alerts",
                 "S-Prices", "S-Prices-actual", "S-Prices-alert",
                 "S-Price-alert", "Price-alert", "S-Price", "S-Price-actual"}

    clean_hdr = []
    hdr_map = {}
    for i, h in enumerate(hdr):
        if h not in skip_cols:
            new_h = h.replace("PnL-actual", "PnL(a)").replace("PnL$-actual", "$(a)").replace("PnL-alert", "PnL(e)").replace("PnL$-alert", "$(e)")
            clean_hdr.append(new_h)
            hdr_map[i] = new_h

    pnl_cols_orig = {i for i, h in enumerate(hdr) if h not in skip_cols and ("PnL" in h or "$" in h)}

    with dpg.table(parent="port_content", header_row=True, resizable=True,
                   policy=dpg.mvTable_SizingFixedFit, borders_outerH=True,
                   borders_outerV=True, borders_innerV=True, borders_innerH=True,
                   scrollY=True, height=-80):

        for h in clean_hdr:
            dpg.add_table_column(label=h)

        for r in dt:
            if not r:
                continue
            with dpg.table_row():
                for i, cell in enumerate(r):
                    if i in hdr_map:
                        cell_str = str(cell)
                        if i in pnl_cols_orig:
                            try:
                                v = float(cell_str)
                                dpg.add_text(cell_str, color=fmt_pnl_color(v))
                            except:
                                dpg.add_text(cell_str)
                        else:
                            dpg.add_text(cell_str)

    if dt:
        total = dt[-1] if dt else []
        if total and len(total) > 12:
            with dpg.group(horizontal=True, parent="port_content"):
                dpg.add_text(f"Avg PnL: {total[12]}%  |  Avg(a): {total[18]}%  |  Total $: {total[13]}  |  Total $(a): {total[19]}")

    dpg.add_button(label="Refresh", parent="port_content", callback=build_portfolio)

# ─── Account ───────────────────────────────────────────────────────────
def build_account():
    clear_children("acc_content")

    if bksession is None:
        dpg.add_text("No brokerage connected", parent="acc_content", color=(150, 150, 150))
        return

    try:
        acc_inf, accnt = gg.get_acc_bals(bksession)
    except:
        dpg.add_text("Failed to get account info", parent="acc_content", color=(240, 80, 80))
        return

    with dpg.group(horizontal=True, parent="acc_content"):
        dpg.add_text(f"Net Liq: ${float(accnt['balance']):,.0f}")
        dpg.add_text(f"    Cash: ${float(accnt['cash']):,.0f}")
        dpg.add_text(f"    Avail: ${float(accnt['funds']):,.0f}")

    dpg.add_separator(parent="acc_content")
    dpg.add_text("Broker Positions", parent="acc_content")

    pos, pos_hdr = gg.get_pos(acc_inf)
    if pos and pos[0] != "NoAccount":
        with dpg.table(parent="acc_content", header_row=True, resizable=True,
                       policy=dpg.mvTable_SizingFixedFit, borders_outerH=True,
                       borders_outerV=True, borders_innerV=True, borders_innerH=True,
                       height=200, scrollY=True):
            for h in pos_hdr:
                dpg.add_table_column(label=h)
            for r in pos:
                with dpg.table_row():
                    for c in r:
                        dpg.add_text(str(c))

    dpg.add_separator(parent="acc_content")
    dpg.add_text("Working Orders", parent="acc_content")

    ords, ord_hdr, _ = gg.get_orders(acc_inf)
    if ords and ords[0] not in ("NoAccount", "NoOrders"):
        with dpg.table(parent="acc_content", header_row=True, resizable=True,
                       policy=dpg.mvTable_SizingFixedFit, borders_outerH=True,
                       borders_outerV=True, borders_innerV=True, borders_innerH=True,
                       height=200, scrollY=True):
            for h in ord_hdr:
                dpg.add_table_column(label=h)
            for r in ords:
                with dpg.table_row():
                    for c in r:
                        dpg.add_text(str(c))

    dpg.add_button(label="Refresh", parent="acc_content", callback=build_account)

# ─── Orders ────────────────────────────────────────────────────────────
def build_orders():
    clear_children("ord_content")

    if discord_bot is None or discord_bot.trader is None:
        dpg.add_text("Trader not available", parent="ord_content", color=(150, 150, 150))
        return

    orders = list(discord_bot.trader.recent_orders)

    if not orders:
        dpg.add_text("No recent orders", parent="ord_content", color=(150, 150, 150))
        if discord_bot and discord_bot.trader:
            dpg.add_text("  Waiting for trades...", parent="ord_content", color=(120, 120, 120))
        return

    def ord_action_color(act):
        if act.startswith("BTO") or act == "ENTERED":
            return (80, 220, 100)
        elif act.startswith("STC") or act.startswith("STO"):
            return (240, 200, 80)
        elif act.startswith("BTC"):
            return (240, 150, 80)
        return (200, 200, 200)

    def ord_status_color(st):
        if st in ("FILLED", "EXECUTED"):
            return (80, 220, 100)
        elif st in ("REJECTED", "NOT_ACCEPTED", "EXPIRED"):
            return (240, 80, 80)
        elif st in ("WORKING", "QUEUED", "OPEN"):
            return (240, 220, 80)
        return (200, 200, 200)

    with dpg.table(parent="ord_content", header_row=True, resizable=True,
                   policy=dpg.mvTable_SizingFixedFit, borders_outerH=True,
                   borders_outerV=True, borders_innerV=True, borders_innerH=True,
                   scrollY=True, height=-80):

        dpg.add_table_column(label="Time")
        dpg.add_table_column(label="Action")
        dpg.add_table_column(label="Symbol")
        dpg.add_table_column(label="Qty")
        dpg.add_table_column(label="Filled")
        dpg.add_table_column(label="Price")
        dpg.add_table_column(label="Status")

        for o in orders:
            with dpg.table_row():
                dpg.add_text(str(o.get('time', '')))
                act = str(o.get('action', ''))
                dpg.add_text(act, color=ord_action_color(act))
                dpg.add_text(str(o.get('symbol', '')))
                dpg.add_text(str(o.get('qty', '') or ''))
                dpg.add_text(str(o.get('filled_qty', '') or ''))
                pr = o.get('price')
                dpg.add_text(f"{pr:.2f}" if pr and pr != 'None' else '')
                st = str(o.get('status', ''))
                dpg.add_text(st, color=ord_status_color(st))

    with dpg.group(horizontal=True, parent="ord_content"):
        dpg.add_text(f"  {len(orders)} orders  |  ")
        dpg.add_button(label="Refresh", callback=build_orders)

# ─── Alert Sending ─────────────────────────────────────────────────────
def send_alert():
    msg_text = dpg.get_value("alert_input")
    if not msg_text.strip():
        return

    author = dpg.get_value("alert_author")
    if not author.strip():
        author = "me"

    dpg.set_value("alert_input", "")

    if discord_bot is None:
        add_message("No Discord bot running", (240, 80, 80))
        return

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    new_msg = pd.Series({
        'AuthorID': None,
        'Author': author,
        'Date': date_str,
        'Content': msg_text,
        'Channel': "GUI_user"
    })
    try:
        discord_bot.new_msg_acts(new_msg, from_disc=False)
        add_message(f"Sent: {msg_text[:80]}", (80, 220, 100))
    except Exception as e:
        add_message(f"Error: {e}", (240, 80, 80))

def append_to_alert(sender, app_data, user_data):
    cur = dpg.get_value("alert_input")
    dpg.set_value("alert_input", cur + f" {user_data} ")

# ─── Settings Tab ──────────────────────────────────────────────────────
def build_settings():
    clear_children("sett_content")
    with dpg.child_window(parent="sett_content", autosize_x=True, autosize_y=True,
                          tag="_sett_scroll", horizontal_scrollbar=True):
        for section in cfg.sections():
            if section in _SETTINGS_EXCLUDE:
                continue
            dpg.add_separator()
            dpg.add_text(f"[{section}]", color=(180, 220, 255))
            dpg.add_separator()
            for key in cfg[section]:
                val = cfg[section][key]
                tag = _tag(section, key)
                vtype = _val_type(val)
                if vtype == 'bool':
                    dpg.add_checkbox(label=key, tag=tag, default_value=val.lower() == 'true')
                elif vtype == 'int':
                    dpg.add_input_int(label=key, tag=tag, default_value=int(val), width=200)
                elif vtype == 'float':
                    dpg.add_input_float(label=key, tag=tag, default_value=float(val), width=200)
                else:
                    multiline = '\n' in val
                    dpg.add_input_text(label=key, tag=tag, default_value=val,
                                       width=-1 if multiline else 400,
                                       multiline=multiline, height=80 if multiline else 0)
        dpg.add_separator(); dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Save && Connect Broker", callback=_save_and_connect, width=250, height=40)
            dpg.add_text("", tag="_sett_status", color=(80, 220, 100))

def _save_and_connect():
    global bksession, discord_bot
    for section in cfg.sections():
        if section in _SETTINGS_EXCLUDE:
            continue
        for key in cfg[section]:
            tag = _tag(section, key)
            if not dpg.does_item_exist(tag):
                continue
            vtype = _val_type(cfg[section][key])
            raw = dpg.get_value(tag)
            if vtype == 'bool':
                cfg[section][key] = 'True' if raw else 'False'
            elif vtype == 'int':
                cfg[section][key] = str(raw)
            elif vtype == 'float':
                cfg[section][key] = str(raw)
            else:
                cfg[section][key] = raw
    config_path = _get_config_path()
    with open(config_path, 'w', encoding='utf-8') as f:
        cfg.write(f)
    from DiscordAlertsTrader.configurator import update_port_cols
    update_port_cols()
    if dpg.does_item_exist("_sett_status"):
        dpg.set_value("_sett_status", "Connecting...")

    def _update_status(text, color):
        if dpg.does_item_exist("_sett_status"):
            dpg.set_value("_sett_status", text)
            dpg.configure_item("_sett_status", color=color)

    def _do_connect():
        global bksession, discord_bot
        if bksession is not None:
            try:
                if hasattr(bksession, 'ib') and hasattr(bksession.ib, 'disconnect'):
                    bksession.ib.disconnect()
            except:
                pass
        bksession = None
        try:
            broker_name = cfg['general']['BROKERAGE']
            sess = get_brokerage(name=broker_name)
            if discord_bot is not None:
                discord_bot.connect_broker(sess)
            bksession = sess
            dpg.set_frame_callback(1, lambda: _update_status(f"Connected: {sess.name}", (80, 220, 100)))
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg = f"Broker error: {e}"
            dpg.set_frame_callback(1, lambda: _update_status(msg, (240, 80, 80)))

    threading.Thread(target=_do_connect, daemon=True).start()

# ─── Autorefresh ───────────────────────────────────────────────────────
def auto_refresh():
    global _refresh_count, _last_order_len
    _refresh_count += 1
    tab = dpg.get_value("main_tab")

    if _refresh_count % 30 == 0:
        if tab == "Dashboard":
            build_dashboard()
        elif tab == "Account":
            build_account()

    if discord_bot and discord_bot.trader:
        current_len = len(discord_bot.trader.recent_orders)
        if current_len != _last_order_len:
            _last_order_len = current_len
            if tab == "Orders":
                build_orders()

    try:
        while True:
            msg = trade_queue.get(block=False)
            if isinstance(msg, list) and len(msg) >= 1:
                text = str(msg[0])[:200]
                col = (200, 200, 200)
                if len(msg) >= 3:
                    if msg[2] == "red":
                        col = (240, 80, 80)
                    elif msg[2] == "green":
                        col = (80, 220, 100)
                    if msg[1] == "blue":
                        col = (100, 150, 255)
                elif len(msg) >= 2:
                    if msg[1] == "blue":
                        col = (100, 150, 255)
                    elif msg[1] == "green":
                        col = (80, 220, 100)
                    elif msg[1] == "red":
                        col = (240, 80, 80)
                add_message(text, col)
    except queue.Empty:
        pass

    dpg.set_frame_callback(dpg.get_frame_count() + 1, auto_refresh)

# ─── Layout ────────────────────────────────────────────────────────────
with dpg.window(label="DAlertsTrader v2", tag="main_win", width=1200, height=850,
                no_close=False, on_close=lambda: dpg.stop_dearpygui()):

    with dpg.menu_bar():
        with dpg.menu(label="File"):
            dpg.add_menu_item(label="Exit", callback=lambda: dpg.stop_dearpygui())

    with dpg.tab_bar(tag="main_tab", callback=lambda s, a: (
        build_dashboard() if a == "Dashboard" else
        build_portfolio() if a == "Portfolio" else
        build_orders() if a == "Orders" else
        build_account() if a == "Account" else
        build_settings() if a == "Settings" else None)):

        with dpg.tab(label="Dashboard", tag="Dashboard"):
            with dpg.child_window(tag="dash_content", height=-60):
                dpg.add_text("Starting...", color=(240, 180, 50))

        with dpg.tab(label="Portfolio", tag="Portfolio"):
            with dpg.child_window(tag="port_content", height=-60):
                dpg.add_text("Data loading...", color=(150, 150, 150))

        with dpg.tab(label="Messages", tag="Messages"):
            with dpg.child_window(tag="msg_win", height=-60):
                with dpg.group(tag="msg_content"):
                    dpg.add_text("Ready", color=(150, 150, 150))

        with dpg.tab(label="Orders", tag="Orders"):
            with dpg.child_window(tag="ord_content", height=-60):
                dpg.add_text("Data loading...", color=(150, 150, 150))

        with dpg.tab(label="Account", tag="Account"):
            with dpg.child_window(tag="acc_content", height=-60):
                dpg.add_text("Data loading...", color=(150, 150, 150))

        with dpg.tab(label="Settings", tag="Settings"):
            with dpg.child_window(tag="sett_content", height=-60):
                dpg.add_text("Loading settings...", color=(150, 150, 150))

    with dpg.child_window(height=80, no_scrollbar=True):
        with dpg.group(horizontal=True):
            dpg.add_text("Author:")
            dpg.add_input_text(tag="alert_author", default_value="me", width=120)
            dpg.add_text("  Alert:")
            dpg.add_input_text(tag="alert_input", default_value="", width=450,
                               on_enter=True, callback=send_alert)
            dpg.add_button(label="Send", callback=send_alert)

        with dpg.group(horizontal=True):
            for act in ("BTO", "STC", "STO", "BTC"):
                dpg.add_button(label=act, width=50, callback=append_to_alert, user_data=act)
            dpg.add_button(label="ExitUpd", width=60, callback=append_to_alert, user_data="Exit Update")

dpg.set_primary_window("main_win", True)

def gui():
    global bksession, discord_bot

    # Set proxy env vars before anything
    if 'proxy' in cfg:
        http_proxy = cfg['proxy'].get('http', '').strip()
        https_proxy = cfg['proxy'].get('https', '').strip()
        if http_proxy:
            os.environ['HTTP_PROXY'] = http_proxy
        if https_proxy:
            os.environ['HTTPS_PROXY'] = https_proxy
        if http_proxy or https_proxy:
            print(f"  Proxy: http={http_proxy or '(none)'} https={https_proxy or '(none)'}")

    print("1: Starting Discord bot ...")
    try:
        trade_q = queue.Queue()
        discord_bot = DiscordBot(trade_q, brokerage=None, cfg=cfg)
        t = threading.Thread(target=lambda: (
            discord_bot.run(cfg['discord']['discord_token'])
            if len(cfg['discord']['discord_token']) > 50
            else print("No Discord token")
        ), daemon=True)
        t.start()
        def poll_discord():
            while True:
                try:
                    msg = trade_q.get(timeout=1)
                    trade_queue.put(msg)
                except queue.Empty:
                    pass
        threading.Thread(target=poll_discord, daemon=True).start()
        print("2: Discord bot thread started")
    except Exception as e:
        print(f"2: Bot error: {e}")

    print("3: Starting GUI ...")
    dpg.create_viewport(title="DAlertsTrader v2", width=1200, height=850)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_frame_callback(0, lambda: [build_settings(), build_dashboard(), auto_refresh()])
    dpg.start_dearpygui()
    dpg.destroy_context()

if __name__ == "__main__":
    gui()
