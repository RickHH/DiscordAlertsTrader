#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Feb 27 09:26:06 2021

@author: adonay
"""
import os.path as op
import queue
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from colorama import Back, Fore

from DiscordAlertsTrader.configurator import cfg
from DiscordAlertsTrader.message_parser import (
    ordersymb_to_str,
    parse_exit_plan,
    set_exit_price_type,
)


def find_last_trade(order, trades_log, open_only=True):
    trades_authr = trades_log["Trader"] == order["Trader"]
    trades_log = trades_log.loc[trades_authr]

    msk_ticker = trades_log["Symbol"].str.match(f"{order['Symbol']}$")

    # Order ticker without dates and strike
    if sum(msk_ticker) == 0 and order["asset"] == "option":
        trades_log = trades_log[trades_log["Asset"] == "option"]
        trade_symb = trades_log["Symbol"].apply(lambda x: x.split("_")[0])

        msk_ticker = trade_symb.str.match(f"{order['Symbol']}$")

    if sum(msk_ticker) == 1:
        (last_trade,) = trades_log[msk_ticker].index.values
    # Either take open trade or last
    elif sum(msk_ticker) > 1:
        open_trade = trades_log.loc[msk_ticker, "isOpen"]
        if open_trade.sum() == 1:
            (last_trade,) = open_trade.index[open_trade == 1]
        elif open_trade.sum() > 1:
            # raise ValueError ("Trade with more than one open position")
            last_trade = open_trade.index[open_trade == 1][-1]
        elif open_trade.sum() == 0:
            last_trade = open_trade.index[-1]
    else:
        return None, 0

    isOpen = trades_log.loc[last_trade, "isOpen"]

    if open_only and isOpen == 0:
        return None, 0
    else:
        return last_trade, isOpen


class AlertsTrader:
    def __init__(
        self,
        brokerage,
        portfolio_fname=cfg["portfolio_names"]["portfolio_fname"],
        alerts_log_fname=cfg["portfolio_names"]["alerts_log_fname"],
        queue_prints=queue.Queue(maxsize=10),
        update_portfolio=True,
        cfg=cfg,
    ):
        self.bksession = brokerage
        self.portfolio_fname = portfolio_fname
        self.alerts_log_fname = alerts_log_fname
        self.queue_prints = queue_prints
        self.send_alert_to_discord = cfg["discord"].getboolean(
            "notify_alerts_to_discord"
        )
        self.discord_channel = (
            None  # discord channel object to post trade alerts, passed on_ready discord
        )
        self.cfg = cfg
        self.EOD = {}  # end of day shorting actions
        self.order_update_rate = 10
        self.max_stc_orders = int(cfg["order_configs"]["max_stc_orders"]) + 1
        self._dynamic_pt_last_minute = {}
        # load port and log
        if op.exists(self.portfolio_fname):
            self.portfolio = pd.read_csv(self.portfolio_fname, na_values=[""])
        else:
            self.portfolio = pd.DataFrame(
                columns=self.cfg["col_names"]["portfolio"].split(",")
            )
            self.portfolio.to_csv(self.portfolio_fname, index=False)
        if op.exists(self.alerts_log_fname):
            self.alerts_log = pd.read_csv(self.alerts_log_fname, na_values=[""])
        else:
            self.alerts_log = pd.DataFrame(
                columns=self.cfg["col_names"]["alerts_log"].split(",")
            )
            self.alerts_log.to_csv(self.alerts_log_fname, index=False)

        self.recent_orders = deque(maxlen=100)
        self.update_portfolio = update_portfolio
        self.update_paused = False
        if update_portfolio:
            # first do a synch, then thread it
            self.update_orders()
            self.updater = threading.Thread(target=self.trade_updater, daemon=True)
            self.updater.start()
            self.queue_prints.put(
                [
                    f"Updating portfolio orders every {self.order_update_rate} secs",
                    "",
                    "green",
                ]
            )
            print(
                Back.GREEN
                + f"Updating portfolio orders every {self.order_update_rate} secs"
            )

    def trade_updater(self):
        while self.update_portfolio is True:
            if self.update_paused is True:
                time.sleep(0.5)
                continue
            t0 = time.time()
            try:
                self.update_orders()
            except Exception as ex:
                str_msg = (
                    f"Error raised during port update, trying again later. Error: {ex}"
                )
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])

            if time.time() - t0 < self.order_update_rate:
                time.sleep(self.order_update_rate - (time.time() - t0))

        str_msg = "Closed portfolio updater"
        print(Back.GREEN + str_msg)
        self.queue_prints.put([str_msg, "", "green"])

    def save_logs(self, csvs=["port", "alert"]):
        if "port" in csvs:
            self.portfolio.to_csv(self.portfolio_fname, index=False)
        if "alert" in csvs:
            self.alerts_log.to_csv(self.alerts_log_fname, index=False)

    def sync_ibkr_positions(self):
        from datetime import datetime

        acc_inf = self.bksession.get_account_info()
        if acc_inf is None:
            print("[sync] No account info")
            return False
        positions = acc_inf.get("securitiesAccount", {}).get("positions", [])
        if not positions:
            print("[sync] No positions found")
            return False

        imported = 0
        for pos in positions:
            sym = pos.get("fullSymbol") or pos.get("symbol", "")
            if not sym:
                continue
            existing = self.portfolio[self.portfolio["Symbol"] == sym]
            if len(existing) > 0 and existing["isOpen"].iloc[0] == 1:
                continue
            qty = float(pos.get("longQuantity", 0))
            if qty <= 0:
                continue
            price = float(pos.get("averagePrice", 0))
            asset = "option" if pos.get("assetType") == "OPT" else "stock"
            if asset == "option":
                price = price / 100
            entry = {
                "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                "Symbol": sym,
                "Trader": "sync",
                "isOpen": 1,
                "BTO-Status": "FILLED",
                "Asset": asset,
                "Type": "BTO",
                "Price": price,
                "Price-alert": price,
                "Price-actual": price,
                "Qty": qty,
                "filledQty": qty,
                "exit_plan": '{"PT1": None, "PT2": None, "PT3": None, "SL": None}',
                "ordID": "",
            }
            self.portfolio = pd.concat(
                [self.portfolio, pd.DataFrame([entry])], ignore_index=True
            )
            imported += 1
            print(f"[sync] Imported {sym} x{qty} @{price}")

        if imported:
            self.save_logs("port")
            print(f"[sync] Imported {imported} positions")
        return imported > 0

    def _add_order_result(
        self, action, symbol, qty, filled_qty, price, status, order_id=None, info=""
    ):
        self.recent_orders.appendleft(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": action,
                "symbol": symbol,
                "qty": qty,
                "filled_qty": filled_qty or 0,
                "price": price,
                "status": status,
                "order_id": order_id,
                "info": info,
            }
        )

    def order_to_pars(self, order):
        pars_str = f"{order['action']} {order['Symbol']} @{order['price']}"
        if order["action"] in ["BTO", "STO"]:
            for i in range(1, self.max_stc_orders):
                pt = f"PT{i}"
                if pt in order.keys() and order[pt] is not None:
                    pars_str = pars_str + f" {pt}: {order[pt]}"
                if "SL" in order.keys() and order["SL"] is not None:
                    pars_str = pars_str + f" SL: {order['SL']}"
        elif order["action"] in ["STC", "BTC"]:
            pars_str = pars_str + f" Qty:{order['Qty']}({int(order['xQty']*100)}%)"
        return pars_str

    def disc_notifier(self, order_info, action=None):
        from discord_webhook import DiscordWebhook

        if not self.send_alert_to_discord:
            return

        if order_info["status"] not in ["FILLED", "EXECUTED", "INDIVIDUAL_FILLS"]:
            print("order in notifier not filled")
            return

        if order_info.get("orderLegCollection") is None:
            if order_info["childOrderStrategies"][0]["status"] == "FILLED":
                order_info = order_info["childOrderStrategies"][0]
            elif order_info["childOrderStrategies"][1]["status"] == "FILLED":
                order_info = order_info["childOrderStrategies"][1]

        if action is None:
            if order_info["orderLegCollection"][0]["instruction"] in [
                "BUY_TO_OPEN",
                "BUY",
                "BUY_OPEN",
            ]:
                action = "BTO"
            elif order_info["orderLegCollection"][0]["instruction"] in [
                "SELL_TO_CLOSE",
                "SELL",
                "SELL_CLOSE",
            ]:
                action = "STC"
            elif order_info["orderLegCollection"][0]["instruction"] in [
                "SELL_TO_OPEN",
                "SELL_SHORT",
                "SELL_OPEN",
            ]:
                action = "STO"
            elif order_info["orderLegCollection"][0]["instruction"] in [
                "BUY_TO_CLOSE",
                "BUY_TO_COVER",
                "BUY_CLOSE",
            ]:
                action = "BTC"

        symbol = ordersymb_to_str(
            order_info["orderLegCollection"][0]["instrument"]["symbol"]
        )
        msg = f"{action} {int(order_info['filledQuantity'])} {symbol} @{order_info.get('price')}"

        if len(self.cfg["discord"]["webhook"]):
            proxy_url = self.cfg["proxy"].get("http", "")
            kwargs = {}
            if proxy_url:
                kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            webhook = DiscordWebhook(
                url=self.cfg["discord"]["webhook"],
                username=self.cfg["discord"]["webhook_name"],
                content=f"{msg.upper()}",
                rate_limit_retry=True,
                **kwargs,
            )
            webhook.execute()
            print("webhook sent")

    def price_now(self, symbol, price_type="BTO", pflag=0):
        "pflag: 0: return str, 1: return float"
        "price_type: BTO, STO, BTC, STC, last"
        if price_type in ["BTO", "BTC"]:
            ptype = "askPrice"
        elif price_type == "last":
            ptype = "lastPrice"
        else:
            ptype = "bidPrice"
        try:
            resp = self.bksession.get_quotes([symbol])
            if (
                resp is None
                or len(resp) == 0
                or symbol not in resp.keys()
                or resp[symbol].get("description") == "Symbol not found"
            ):
                str_msg = f"{symbol} not found during price quote"
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                quote = -1
            else:
                quote = resp[symbol][ptype]
        except KeyError as e:
            str_msg = f"price_now error for symbol {symbol}: {e}.\n Try again later"
            print(Back.RED + str_msg)
            self.queue_prints.put([str_msg, "", "red"])
            quote = -1
        except Exception as e:
            str_msg = f"price_now exception for {symbol}: {e}"
            print(Back.RED + str_msg)
            self.queue_prints.put([str_msg, "", "red"])
            quote = -1
        if pflag:
            return quote
        else:
            return "CURRENTLY @%.2f" % quote

    def confirm_and_send(self, order, pars, order_funct):
        resp, order, ord_chngd = self.notify_alert(order, pars)
        print(f"[TRACE confirm] notify_alert returned resp='{resp}'")
        if resp in ["yes", "y"]:
            try:
                order_kwargs = order_funct(**order)
                print(f"[TRACE confirm] order_funct returned: {order_kwargs}")
            except Exception as e:
                str_msg = f"Error building order: {e}"
                print(f"[TRACE confirm] order_funct raised: {e}")
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                return None, None, order, None
            if order_kwargs is None:
                str_msg = "Order function returned None (likely symbol not found)"
                print(f"[TRACE confirm] order_kwargs=None → no trade")
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                return None, None, order, None
            if order_kwargs.get("conId") is None:
                print(
                    f"[TRACE confirm] No conId, sending order with contract details instead"
                )
            print(
                f"[TRACE confirm] Calling send_order with conId={order_kwargs.get('conId')}..."
            )
            try:
                ord_resp, ord_id = self.bksession.send_order(order_kwargs)
            except Exception as e:
                str_msg = f"Error in order {e}"
                print(f"[TRACE confirm] send_order raised: {e}")
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                return None, None, order, None

            print(f"[TRACE confirm] send_order returned: resp={ord_resp}, id={ord_id}")
            if ord_resp is None:
                print(f"[TRACE confirm] ord_resp=None → no trade")
                return None, None, order, None

            str_msg = f"Sent order {order['action']} {order['Qty']} {order['Symbol']} @{order['price']}"
            print(Back.GREEN + str_msg)
            color = "green" if order["action"] == "BTO" else "yellow"
            self.queue_prints.put([str_msg, "", color])
            return ord_resp, ord_id, order, ord_chngd

        elif resp in ["no", "n"]:
            return None, None, order, None

    def short_orders(self, order, pars):
        if order["action"] == "STO":
            if order["asset"] == "option":
                # check strike
                strike = eval(re.split("C|P", order["Symbol"].split("_")[1])[1])
                if strike > eval(self.cfg["shorting"]["max_strike"]):
                    str_msg = f"STO strike too high: {strike}, order aborted"
                    print(Back.RED + str_msg)
                    self.queue_prints.put([str_msg, "", "red"])
                    return "no", order, False

                # check DTE
                if len(self.cfg["shorting"]["max_dte"]):
                    if order.get("dte") is None:
                        exp_dt = datetime.strptime(
                            f"{order['expDate']}/{datetime.now().year}", "%m/%d/%Y"
                        ).date()
                        dt = datetime.now().date()
                        order["dte"] = (exp_dt - dt).days
                    if order["dte"] > int(self.cfg["shorting"]["max_dte"]):
                        str_msg = f"STO {order['dte']} DTE larger than max in config: {self.cfg['shorting']['max_dte']}, order aborted"
                        print(Back.RED + str_msg)
                        self.queue_prints.put([str_msg, "", "red"])
                        return "no", order, False

                # check if above min price
                if len(self.cfg["shorting"]["min_price"]):
                    min_price = float(self.cfg["shorting"]["min_price"])
                    if (order["price"] * 100) < min_price:
                        str_msg = (
                            f"STO price too low: {order['price']*100}, order aborted"
                        )
                        print(Back.RED + str_msg)
                        self.queue_prints.put([str_msg, "", "red"])
                        return "no", order, False

            # use current price as ask, bid, mid, last, or alert
            sto_price = cfg["shorting"]["STO_price"]
            check_price = True
            if sto_price == "alert":
                check_price = False
            ptype = (
                "BTO"
                if sto_price in ["ask", "mid"]
                else "last"
                if sto_price == "last"
                else "STO"
            )
            if check_price:
                order["price_actual"] = self.price_now(order["Symbol"], ptype, 1)
            else:
                order["price_actual"] = order["price"]
            pdiff = round(
                (order["price"] - order["price_actual"]) / order["price"] * 100, 1
            )

            if self.cfg["shorting"]["STO_trailingstop"] != "":
                # if pdiff too large, trigger trailing only when price target
                if pdiff > eval(self.cfg["shorting"]["max_price_diff"]):
                    order["price_trigger"] = order["price_actual"]
                    str_msg = f"STO alert price diff too high: {pdiff}% at {order['price_actual']}, trailing will trigger at {order['price']}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                trail = (float(self.cfg["shorting"]["STO_trailingstop"]) / 100) * order[
                    "price_actual"
                ]
                order["trail_stop_const"] = -round(trail / 0.01) * 0.01

            else:
                # if price diff not too high, use current price
                if pdiff < eval(self.cfg["shorting"]["max_price_diff"]):
                    if check_price:
                        if sto_price == "mid":
                            p1 = self.price_now(order["Symbol"], "BTO", 1)
                            p2 = self.price_now(order["Symbol"], "STO", 1)
                            order["price"] = (p1 + p2) / 2
                            if order["price"] < 1 and order["asset"] == "stock":
                                order["price"] = round(order["price"], 3)
                            else:
                                order["price"] = round(order["price"], 2)
                        else:
                            order["price"] = order["price_actual"]
                    else:
                        order["price"] = order["price_actual"]
                else:
                    str_msg = f"STO alert price diff too high: {pdiff}% at {order['price_actual']}, keeping original price of {order['price']}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])

            # Handle quantity
            if order.get("Qty") is not None and not self.cfg["shorting"].getboolean(
                "ignore_alert_qty"
            ):
                order["trader_qty"] = order["Qty"]
            elif self.cfg["shorting"]["default_sto_qty"] == "buy_one":
                order["Qty"] = 1
            elif (
                self.cfg["shorting"]["default_sto_qty"] == "margin_capital"
                and order["asset"] == "option"
            ):
                order["Qty"] = max(
                    1, int(eval(self.cfg["shorting"]["margin_capital"]) / (20 * strike))
                )
            elif (
                self.cfg["shorting"]["default_sto_qty"] == "trade_capital"
                or order["asset"] == "stock"
            ):
                if order["asset"] == "option":
                    order["Qty"] = int(
                        max(
                            round(
                                float(self.cfg["shorting"]["trade_capital"])
                                / (100 * order["price"])
                            ),
                            1,
                        )
                    )
                else:
                    order["Qty"] = int(
                        max(
                            float(self.cfg["shorting"]["trade_capital"])
                            // order["price"],
                            1,
                        )
                    )

            # Handle trade cost lims
            max_trade_val = float(self.cfg["shorting"]["max_trade_capital"])
            if (
                100 * order["price"] * order["Qty"] > max_trade_val
                and order["asset"] == "option"
            ):
                Qty_ori = order["Qty"]
                order["Qty"] = int(max(max_trade_val // (100 * order["price"]), 1))
                if order["price"] * order["Qty"] <= max_trade_val:
                    str_msg = f"STO trade exeeded max_trade_capital of ${max_trade_val}, order quantity reduced to {order['Qty']} from {Qty_ori}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                else:
                    str_msg = f"cancelled STO: trade exeeded max_trade_capital of ${max_trade_val}"
                    print(Back.RED + str_msg)
                    self.queue_prints.put([str_msg, "", "red"])
                    return "no", order, False
            elif (
                100 * order["price"] * order["Qty"]
                < float(self.cfg["shorting"]["min_trade_capital"])
                and order["asset"] == "option"
            ):
                str_msg = f"STO trade below min_trade_capital of ${self.cfg['shorting']['min_trade_capital']}, order aborted"
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                return "no", order, False
            elif (
                order["asset"] == "stock"
                and order["price"] * order["Qty"] > max_trade_val
            ):
                str_msg = f"STO trade below min_trade_capital of ${self.cfg['shorting']['min_trade_capital']}, order aborted"
                print(Back.RED + str_msg)
                self.queue_prints.put([str_msg, "", "red"])
                return "no", order, False
            return "yes", order, False
        # decide if do BTC based on alert
        elif order["action"] == "BTC":
            return "yes", order, False
        else:
            return "no", order, False

    def notify_alert(self, order, pars):
        price_now = self.price_now
        symb = order["Symbol"]
        ord_ori = order.copy()
        pars_ori = pars
        act = order["action"]
        ix_a = 0
        while True:
            if ix_a == 0 and order.get("price_actual", 0) > 0:
                actual_price = order["price_actual"]
                ix_a += 1
            else:
                # If symbol not found, quote val returned is -1
                actual_price = price_now(symb, act, 1)
            if actual_price == -1:
                if self.cfg["order_configs"].getboolean("auto_trade"):
                    print(
                        f"[TRACE notify] price_unavailable, using alert price for auto-trade"
                    )
                    actual_price = ord_ori["price"]
                    order["price_actual"] = actual_price
                    pdiff = 0.0
                    question = f"{pars_ori} (price unavailable, using alert price)"
                    # skip to auto_trade block below
                else:
                    return "no", order, False
            else:
                order["price_actual"] = actual_price
                pdiff = (actual_price - ord_ori["price"]) / ord_ori["price"]
                pdiff = round(pdiff * 100, 1)
                question = f"{pars_ori} currently @ {actual_price}"

            if order["action"] in ["STO", "BTC"]:
                return self.short_orders(order, pars)

            elif self.cfg["order_configs"].getboolean("sell_current_price"):
                if (
                    pdiff
                    < eval(self.cfg["order_configs"]["max_price_diff"])[order["asset"]]
                ):
                    order["price"] = actual_price
                    if order["action"] in ["BTO", "STC"]:
                        # reduce 1% to ensure fill
                        if order["action"] == "BTO":
                            if order["price"] < 1 and order["asset"] == "stock":
                                new_price = round(order["price"] * 1.05, 3)
                            else:
                                new_price = round(order["price"] * 1.05, 2)
                        elif order["action"] == "STC":
                            if order["price"] < 1 and order["asset"] == "stock":
                                new_price = round(order["price"] * 0.95, 3)
                            else:
                                new_price = round(order["price"] * 0.95, 2)

                    order["price"] = self.round_price(new_price, order)

                    pars = self.order_to_pars(order)
                    question += f"\n new price: {pars}"
                else:
                    if (
                        self.cfg["order_configs"].getboolean("auto_trade") is True
                        and order["action"] == "BTO"
                    ):
                        str_msg = f"BTO alert price diff too high: {pdiff}% at {actual_price}, keeping original price of {ord_ori['price']}"
                        print(Back.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "green"])

            if self.cfg["order_configs"].getboolean("auto_trade") is True:
                print(f"[TRACE notify] auto_trade=True, action={order['action']}")
                if (
                    self.cfg["general"].getboolean("DO_BTO_TRADES") is False
                    and order["action"] == "BTO"
                ):
                    str_msg = (
                        f"BTO not accepted by config options: DO_BTO_TRADES = False"
                    )
                    print(f"[TRACE notify] DO_BTO_TRADES=False → return no")
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    return "no", order, False

                elif order["action"] == "BTO":
                    print(f"[TRACE notify] Processing BTO...")
                    if len(cfg["order_configs"]["exclude_tickers"]):
                        no_trade = cfg["order_configs"]["exclude_tickers"].split(",")
                        no_trade = [i.strip() for i in no_trade]
                        if order["Symbol"].split("_")[0] in no_trade:
                            str_msg = f"BTO not accepted by config options: exclude_tickers = {no_trade}"
                            print(
                                f"[TRACE notify] Symbol in exclude_tickers → return no"
                            )
                            print(Back.GREEN + str_msg)
                            self.queue_prints.put([str_msg, "", "green"])
                            return "no", order, False

                    price = order["price"]
                    if price == 0:
                        str_msg = f"Order not accepted price is 0"
                        print(f"[TRACE notify] price=0 → return no")
                        print(Back.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "red"])
                        return "no", order, False
                    price = price * 100 if order["asset"] == "option" else price

                    max_trade_vals = eval(
                        self.cfg["order_configs"]["max_trade_capital"]
                    )
                    max_trade_val = float(
                        max_trade_vals.get(order["Trader"], max_trade_vals["default"])
                    )
                    default_bto_qtys = eval(
                        self.cfg["order_configs"]["default_bto_qty"]
                    )
                    default_bto_qty = default_bto_qtys.get(
                        order["Trader"], default_bto_qtys["default"]
                    )
                    trade_capitals = eval(self.cfg["order_configs"]["trade_capital"])
                    trade_capital = float(
                        trade_capitals.get(order["Trader"], trade_capitals["default"])
                    )

                    if "Qty" not in order.keys() or order["Qty"] is None:
                        print(
                            f"[TRACE notify] Qty not in order, using default_bto_qty={default_bto_qty}"
                        )
                        if default_bto_qty == "buy_one":
                            order["Qty"] = 1
                        elif default_bto_qty == "trade_capital":
                            order["Qty"] = int(max(round(trade_capital / price), 1))

                    print(
                        f"[TRACE notify] price={order['price']}(*100={price}), Qty={order['Qty']}, max_trade_val={max_trade_val}"
                    )
                    if price * order["Qty"] > max_trade_val:
                        Qty_ori = order["Qty"]
                        order["Qty"] = int(max(max_trade_val // price, 1))
                        if price * order["Qty"] <= max_trade_val:
                            str_msg = f"BTO trade exeeded max_trade_capital of ${max_trade_val}, order quantity reduced to {order['Qty']} from {Qty_ori}"
                            print(
                                f"[TRACE notify] Reduced Qty {Qty_ori}→{order['Qty']} due to max_trade_capital"
                            )
                            print(Back.GREEN + str_msg)
                            self.queue_prints.put([str_msg, "", "green"])
                            order["trader_qty"] = Qty_ori
                        else:
                            str_msg = f"cancelled BTO: trade exeeded max_trade_capital of ${max_trade_val}"
                            print(
                                f"[TRACE notify] Even 1 contract exceeds max_trade_capital → return no"
                            )
                            print(Back.RED + str_msg)
                            self.queue_prints.put([str_msg, "", "red"])
                            return "no", order, False
                print(f"[TRACE notify] All auto-trade checks passed → return yes")
                return "yes", order, False

            # Manual trade
            resp = input(
                Back.RED + question + "\n Make trade? (y, n or (c)hange) \n"
            ).lower()

            if resp in ["c", "change", "y", "yes"] and "Qty" not in order.keys():
                order["Qty"] = int(
                    input(
                        "Order qty not available."
                        + f" How many units to buy? {price_now(symb, act)} \n"
                    )
                )

            if resp in ["c", "change"]:
                new_order = order.copy()
                new_order["price"] = float(
                    input(
                        f"Change price @{order['price']}"
                        + f" {price_now(symb, act)}? Leave blank if NO \n"
                    )
                    or order["price"]
                )

                if order["action"] == "BTO":
                    PTs = [order[f"PT{i}"] for i in range(1, self.max_stc_orders)]
                    PTs = eval(
                        input(
                            f"Change PTs @{PTs} {price_now(symb, act)}? \
                                      Leave blank if NO, respond eg [1, 2, None] \n"
                        )
                        or str(PTs)
                    )

                    new_n = len([i for i in PTs if i is not None])
                    if new_n != order["n_PTs"]:
                        new_order["n_PTs"] = new_n
                        new_order["PTs_Qty"] = [
                            round(1 / new_n, 2) for i in range(new_n)
                        ]
                        new_order["PTs_Qty"][-1] = new_order["PTs_Qty"][-1] + (
                            1 - sum(new_order["PTs_Qty"])
                        )

                    new_order["SL"] = (
                        input(
                            f"Change SL @{order['SL']} {price_now(symb, act)}?"
                            + " Leave blank if NO \n"
                        )
                        or order["SL"]
                    )

                    new_order["SL"] = (
                        eval(new_order["SL"])
                        if isinstance(new_order["SL"], str)
                        else new_order["SL"]
                    )
                order = new_order
                pars = self.order_to_pars(order)
            else:
                break
        ord_chngd = ord_ori != order
        return resp, order, ord_chngd

    def close_open_exit_orders(self, open_trade, STCn=None):
        # close STCn waiting orders
        if STCn is None:
            STCn = range(1, self.max_stc_orders)
        position = self.portfolio.iloc[open_trade]
        if type(STCn) == int:
            STCn = [STCn]

        for i in STCn:
            if pd.isnull(position[f"STC{i}-ordID"]):
                continue

            order_id = position[f"STC{i}-ordID"]
            ord_stat, _ = self.get_order_info(order_id)

            if ord_stat not in [
                "FILLED",
                "EXECUTED",
                "CANCELED",
                "CANCEL_REQUESTED",
                "REJECTED",
                "EXPIRED",
            ]:
                print(Back.GREEN + f"Cancelling {position['Symbol']} STC{i}")
                self.queue_prints.put(
                    [f"Cancelling {position['Symbol']} STC{i}", "", "green"]
                )
                _ = self.bksession.cancel_order(order_id)

                self.portfolio.loc[open_trade, f"STC{i}-Status"] = np.nan
                self.portfolio.loc[open_trade, f"STC{i}-ordID"] = np.nan
                self.save_logs("port")

            elif ord_stat in ["REJECTED", "CANCELED", "CANCEL_REQUESTED", "EXPIRED"]:
                self.portfolio.loc[open_trade, f"STC{i}-Status"] = np.nan
                self.portfolio.loc[open_trade, f"STC{i}-ordID"] = np.nan
                self.save_logs("port")

    def get_order_info(self, order_id):
        # try:
        order_status, order_info = self.bksession.get_order_info(order_id)
        return order_status, order_info

    # except Exception as ex:
    #     print(f"Caught Error in order info, skipping order info retr. Error: {ex}")
    #     self.queue_prints.put([f"Caught Error, skipping order info retr. Error: {ex}", "", "red"])
    #     return None, None

    ######################################################################
    # ALERT TRADER
    ######################################################################

    def new_trade_alert(self, order: dict, pars: str, msg):
        """get order from ```parser_alerts```"""
        print(
            f"[TRACE new_trade] action={order['action']} asset={order.get('asset')} symb={order['Symbol']} price={order.get('price')} Qty={order.get('Qty')}"
        )

        open_trade, isOpen = find_last_trade(order, self.portfolio)
        print(
            f"[TRACE new_trade] find_last_trade: open_trade={open_trade}, isOpen={isOpen}"
        )

        time_strf = "%Y-%m-%d %H:%M:%S.%f"
        date = datetime.now().strftime(time_strf)

        log_alert = {
            "Date": date,
            "Symbol": order["Symbol"],
            "Trader": order["Trader"],
            "parsed": pars,
            "msg": msg,
        }

        if order["action"] == "ExitUpdate" and isOpen:
            if order.get("isopen") == False:
                # close position command
                self.close_open_exit_orders(open_trade)
                self.portfolio.loc[open_trade, "isOpen"] = 0
                self.save_logs("port")

                symb = self.portfolio.loc[open_trade, "Symbol"]
                msg = f"Position marked as closed: {symb}"
                print(Back.GREEN + msg)
                self.queue_prints.put([msg, "", "green"])
                return

            elif order.get("cancelavg"):
                # cancel avg price
                trade = self.portfolio.loc[open_trade]
                if not pd.isnull(trade["avgID"]):
                    if isinstance(trade["ordID"], str):
                        ordID = trade["ordID"].split(",")[-1]
                    else:
                        ordID = trade["ordID"]
                    order_status = self.bksession.cancel_order(ordID)
                    order_status = order_status.replace("UROUT", "CANCELED")
                    self.portfolio.loc[i, "BTO-avg-Status"] = order_status

                    symb = self.portfolio.loc[open_trade, "Symbol"]
                    msg = f"Cancelled avg order for {symb}"
                    print(Back.GREEN + msg)
                    self.queue_prints.put([msg, "", "green"])
                return

            # Pause updater to avoid overlapping
            self.update_paused = True

            old_plan = self.portfolio.loc[open_trade, "exit_plan"]
            new_plan = parse_exit_plan(order)

            # check if asset if price stock or contract
            if self.portfolio.loc[open_trade, "Asset"] == "option":
                new_plan["price"] = self.portfolio.loc[open_trade, "Price"]
                sym_inf = self.portfolio.loc[open_trade, "Symbol"].split("_")[1]
                strike = re.split("C|P", sym_inf)[1]
                new_plan["strike"] = strike + "C" if "C" in sym_inf else strike + "P"
                for i in range(1, self.max_stc_orders):
                    exit_price = new_plan.get(f"PT{i}")
                    if exit_price is not None:
                        new_plan[f"PT{i}"] = set_exit_price_type(exit_price, new_plan)
                    if new_plan.get("SL"):
                        new_plan[f"SL"] = set_exit_price_type(
                            new_plan.get("SL"), new_plan
                        )
                _ = [new_plan.pop(k) for k in ["price", "strike"]]

            # Update PT is already STCn
            istc = None
            for i in range(1, 3):
                if not pd.isnull(self.portfolio.loc[open_trade, f"STC{i}-alerted"]):
                    istc = i + 1
            if istc is not None and any(["PT" in k for k in new_plan.keys()]):
                new_plan_c = new_plan.copy()
                for i in range(1, self.max_stc_orders):
                    if new_plan.get(f"PT{i}"):
                        del new_plan_c[f"PT{i}"]
                        new_plan_c[f"PT{istc}"] = new_plan[f"PT{i}"]
                        # Cancel orders previous plan if any
                        self.close_open_exit_orders(open_trade, istc)
                new_plan = new_plan
            else:
                # Cancel orders previous plan if any
                self.close_open_exit_orders(open_trade)

            renew_plan = eval(old_plan)
            if renew_plan is not None or renew_plan != {}:
                for k in new_plan.keys():
                    renew_plan[k] = new_plan[k]
            else:
                renew_plan = new_plan

            self.portfolio.loc[open_trade, "exit_plan"] = str(renew_plan)
            self.update_paused = False

            log_alert["action"] = "ExitUpdate"
            self.save_logs()

            symb = self.portfolio.loc[open_trade, "Symbol"]
            print(
                Back.GREEN
                + f"Updated {symb} exit plan from :{old_plan} to {renew_plan}"
            )
            self.queue_prints.put(
                [
                    f"Updated {symb} exit plan from :{old_plan} to {renew_plan}",
                    "",
                    "green",
                ]
            )
            return

        elif order["action"] in ["BTO", "STO"] and not isOpen:
            alert_price = order["price"]
            action = order["action"]

            # Get exit plan and add default vals if needed
            exit_plan = parse_exit_plan(order)
            if action == "BTO":
                if (
                    len(self.cfg["order_configs"]["default_exits"])
                    and exit_plan.get("PT1") is None
                    and exit_plan.get("SL") is None
                ):
                    exit_plan = eval(self.cfg["order_configs"]["default_exits"])
            # Do BTO TrailingStop
            if order.get("open_trailingstop"):
                # get TS value, convet from percentage if needed
                ts = (
                    order.get("open_trailingstop")
                    .replace("invTSbuy ", "")
                    .replace("TSbuy ", "")
                )
                if isinstance(ts, str) and "%" in ts:
                    pricenow = self.price_now(order["Symbol"], "BTO", 1)
                    ts = round((float(ts.split("%")[0]) / 100) * pricenow, 2)
                elif isinstance(ts, str):
                    ts = eval(ts)
                    if (
                        ts / order["price"] > 10
                    ):  # must be error, diff too big, make it %
                        str_msg = f"Trailing stop too high ({ts/order['price']} diff), must be in %, converted to {ts/100}%"
                        print(Back.RED + str_msg)
                        self.queue_prints.put([str_msg, "", "red"])
                        pricenow = self.price_now(order["Symbol"], "BTO", 1)
                        ts = round((ts / 100) * pricenow, 2)

                ts_order = order.get("open_trailingstop")
                if ts_order.startswith("invTSbuy"):
                    new_trade = {
                        "Date": date,
                        "Symbol": order["Symbol"],
                        "isOpen": 1,
                        "BTO-Status": "invTSbuy",
                        "Asset": order["asset"],
                        "Type": action,
                        "Qty": 0,
                        "Price-alert": alert_price,
                        "Price-actual": pricenow,
                        "exit_plan": str(exit_plan),
                        "Trader": order["Trader"],
                        "Risk": order["risk"],
                        "open_trailingstop": f"ts:{ts},max_price:{pricenow}",
                        "trader_qty": order.get("Qty", 1),
                    }
                    self.portfolio = pd.concat(
                        [
                            self.portfolio,
                            pd.DataFrame.from_records(new_trade, index=[0]),
                        ],
                        ignore_index=True,
                    )
                    str_msg = f"{action} {order['Symbol']} created inverse TS local order @{pricenow}, TSconst {ts}, stp @{pricenow-ts}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    self.save_logs("port")
                    return
                else:
                    order["trail_stop_const"] = round(ts / 0.01) * 0.01
                    order_response, order_id, order, _ = self.confirm_and_send(
                        order, pars, self.bksession.make_STC_SL_trailstop
                    )

            else:
                if self.bksession.name == "cobra":
                    self.update_paused = True
                print(
                    f"[TRACE BTO] Calling confirm_and_send with make_BTO_lim_order..."
                )
                order_response, order_id, order, _ = self.confirm_and_send(
                    order, pars, self.bksession.make_BTO_lim_order
                )
                print(
                    f"[TRACE BTO] confirm_and_send returned: order_response={order_response}, order_id={order_id}"
                )
                if self.bksession.name == "cobra":
                    self.update_paused = False

            self.save_logs("port")
            if order_response is None:  # Assume trade not accepted
                self._add_order_result(
                    action,
                    order["Symbol"],
                    order.get("Qty"),
                    0,
                    order.get("price"),
                    "NOT_ACCEPTED",
                )
                log_alert["action"] = action + "-notAccepted"
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                str_msg = action + " not accepted by user, order response is none"
                print(Back.GREEN + str_msg)
                self.queue_prints.put([str_msg, "", "green"])
                return

            order_status, order_info = self.get_order_info(order_id)
            if order_info is None and self.bksession.name == "ibkr":
                order_status = "FILLED"
                order_info = order
                order_info["quantity"] = order["Qty"]
                order_info["filledQuantity"] = order["Qty"]
                print("IBKR order was None, assuming filled")

            if order_status == "REJECTED":
                self._add_order_result(
                    action,
                    order["Symbol"],
                    order_info.get("quantity"),
                    0,
                    order_info.get("price"),
                    "REJECTED",
                )
                log_alert["action"] = "REJECTED"
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                print(Back.GREEN + action + " REJECTED")
                self.queue_prints.put([action + " REJECTED", "", "green"])
                return

            if order["action"] == "STO":
                price = order_info.get("price")
                if price is None:
                    price = order_info.get("activationPrice")

            new_trade = {
                "Date": date,
                "Symbol": order["Symbol"],
                "isOpen": 1,
                "BTO-Status": order_status,
                "Qty": order_info["quantity"],
                "Asset": order["asset"],
                "Type": action,
                "Price": order_info.get("price"),
                "Price-alert": alert_price,
                "Price-actual": order["price_actual"],
                "ordID": order_id,
                "exit_plan": str(exit_plan),
                "Trader": order["Trader"],
                "Risk": order["risk"],
                "open_trailingstop": order.get("open_trailingstop"),
                "trader_qty": order_info.get("trader_qty"),
                "original_price": order_info.get("price"),
                "original_qty": order_info.get("quantity"),
            }

            self.portfolio = pd.concat(
                [self.portfolio, pd.DataFrame.from_records(new_trade, index=[0])],
                ignore_index=True,
            )

            if order_status in ["FILLED", "EXECUTED"]:
                ot, _ = find_last_trade(order, self.portfolio)
                self.portfolio.loc[ot, "Price"] = order_info["price"]
                self.portfolio.loc[ot, "filledQty"] = order_info["filledQuantity"]
                self.disc_notifier(order_info)
                if self.portfolio.loc[ot, "Type"] == "STO":
                    price = order_info.get("price")
                    if (
                        len(self.cfg["shorting"]["BTC_PT"])
                        and exit_plan.get("PT1") is None
                    ):
                        exit_plan["PT1"] = round(
                            price * (1 - float(self.cfg["shorting"]["BTC_PT"]) / 100), 2
                        )
                    if (
                        len(self.cfg["shorting"]["BTC_SL"])
                        and exit_plan.get("SL") is None
                    ):
                        exit_plan["SL"] = round(
                            price * (1 + float(self.cfg["shorting"]["BTC_SL"]) / 100), 2
                        )
                    self.portfolio.loc[ot, "exit_plan"] = str(exit_plan)

                # convert % to val exit plan
                self.exit_percent_to_price(ot)

            self._add_order_result(
                action,
                order["Symbol"],
                order_info.get("quantity"),
                order_info.get("filledQuantity"),
                order_info.get("price"),
                order_status,
            )
            str_msg = f"{action} {order['Symbol']} sent @ {order_info.get('price')}. Status: {order_status}"
            print(Back.GREEN + str_msg)
            self.queue_prints.put([str_msg, "", "green"])

            # Log portfolio, trades_log
            log_alert["action"] = action
            log_alert["portfolio_idx"] = len(self.portfolio) - 1
            self.alerts_log = pd.concat(
                [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                ignore_index=True,
            )
            self.save_logs()

        elif (
            order["action"] == "BTO"
            and self.cfg["order_configs"].getboolean("accept_repeated_bto_alerts")
            or order["action"] == "STO"
            and self.cfg["shorting"].getboolean("accept_repeated_sto_alerts")
        ):
            alert_price = order["price"]
            order_response, order_id, order, _ = self.confirm_and_send(
                order, pars, self.bksession.make_BTO_lim_order
            )
            self.save_logs("port")
            if order_response is None:  # Assume trade not accepted
                log_alert["action"] = "BTO-Avg-notAccepted"
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                print(Back.GREEN + "BTO avg not accepted by user")
                self.queue_prints.put(["BTO avg not accepted by user", "", "green"])
                return

            order_status, order_info = self.get_order_info(order_id)
            if order_info is None and self.bksession.name == "ibkr":
                order_status = "FILLED"
                order_info = order
                order_info["quantity"] = order["Qty"]
                order_info["filledQuantity"] = order["Qty"]
                print("IBKR order was None, assuming filled")

            fld_qty = (
                order_info.get("filledQuantity", 0)
                if order_status in ["FILLED", "EXECUTED"]
                else 0
            )
            self._add_order_result(
                "AVG-" + order["action"],
                order["Symbol"],
                order_info.get("quantity"),
                fld_qty,
                order_info.get("price"),
                order_status,
            )
            self.portfolio.loc[open_trade, "BTO-avg-Status"] = order_status
            # if np.integer or int, turn to str
            if isinstance(self.portfolio.loc[open_trade, "ordID"], np.integer):
                self.portfolio.loc[open_trade, "ordID"] = str(
                    self.portfolio.loc[open_trade, "ordID"]
                )
            self.portfolio.loc[open_trade, "ordID"] += f",{order_id}"

            if pd.isnull(self.portfolio.loc[open_trade, "Avged"]):
                self.portfolio.loc[open_trade, "Avged"] = 1
                self.portfolio.loc[open_trade, "Avged-prices-alert"] = alert_price
                self.portfolio.loc[open_trade, "Avged-prices"] = order_info["price"]
                self.portfolio.loc[open_trade, "Avged-Qty"] = order_info["quantity"]
            else:
                self.portfolio.loc[open_trade, "Avged"] += 1
                al_pr = self.portfolio.loc[open_trade, "Avged-prices-alert"]
                av_pr = self.portfolio.loc[open_trade, "Avged-prices"]
                av_qt = self.portfolio.loc[open_trade, "Avged-Qty"]
                self.portfolio.loc[
                    open_trade, "Avged-prices-alert"
                ] = f"{al_pr},{alert_price}"
                self.portfolio.loc[
                    open_trade, "Avged-prices"
                ] = f"{av_pr},{order_info['price']}"
                self.portfolio.loc[
                    open_trade, "Avged-Qty"
                ] = f"{av_qt},{order_info['quantity']}"

            avg = self.portfolio.loc[open_trade, "Avged"]

            self.portfolio.loc[open_trade, "Qty"] += order_info["quantity"]
            if order_status in ["FILLED", "EXECUTED"]:
                or_price = (
                    self.portfolio.loc[open_trade, "Price"]
                    * self.portfolio.loc[open_trade, "filledQty"]
                )
                nw_price = order_info["price"] * order_info["filledQuantity"]
                avg_price = round(
                    (or_price + nw_price)
                    / (
                        self.portfolio.loc[open_trade, "filledQty"]
                        + order_info["filledQuantity"]
                    ),
                    2,
                )
                self.portfolio.loc[open_trade, "Price"] = avg_price

                self.portfolio.loc[open_trade, "filledQty"] += order_info[
                    "filledQuantity"
                ]
                self.disc_notifier(order_info)
                self.close_open_exit_orders(open_trade)
            str_msg = f"BTO {avg} th AVG, {order['Symbol']} sent @{order_info['price']}. Status: {order_status}"
            print(Back.GREEN + str_msg)
            self.queue_prints.put([str_msg, "", "green"])

            if (
                self.portfolio.loc[open_trade, "filledQty"]
                > self.portfolio.loc[open_trade, "Qty"]
            ):
                raise ValueError(
                    f"Filled qty {self.portfolio.loc[open_trade, 'filledQty']} larger than Qty {self.portfolio.loc[open_trade, 'Qty']}"
                )

            # Log portfolio, trades_log
            log_alert["action"] = "BTO-avg"
            log_alert["portfolio_idx"] = len(self.portfolio) - 1
            self.alerts_log = pd.concat(
                [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                ignore_index=True,
            )
            self.save_logs()

        elif order["action"] in ["BTO", "STO"]:
            str_act = f"Repeated {order['action']}"
            log_alert["action"] = f"{order['action']}-Null-Repeated"
            self.alerts_log = pd.concat(
                [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                ignore_index=True,
            )
            self.save_logs(["alert"])
            print(Back.RED + str_act)
            self.queue_prints.put([str_act, "", "red"])

        elif order["action"] in ["STC", "BTC"] and isOpen == 0:
            open_trade, _ = find_last_trade(order, self.portfolio, open_only=False)
            if open_trade is None:
                log_alert[
                    "action"
                ] = str_msg = f"{order['action']}-alerted without open position"
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs()
                if (
                    self.cfg["general"].getboolean("DO_BTO_TRADES")
                    and order["action"] == "STC"
                ) or (
                    self.cfg["general"].getboolean("DO_STO_TRADES")
                    and order["action"] == "BTC"
                ):
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                return

            position = self.portfolio.iloc[open_trade]
            # Check if closed position was not alerted
            for i in range(1, self.max_stc_orders):
                STC = f"STC{i}"
                if pd.isnull(position[f"{STC}-alerted"]):
                    self.portfolio.loc[open_trade, f"{STC}-alerted"] = 1
                    # If alerted and already sold
                    if not pd.isnull(position[f"{STC}-Price"]):
                        print(Back.RED + "Position already closed")
                        self.queue_prints.put(["Position already closed", "", "red"])
                        log_alert["action"] = f"{STC}-alerterdAfterClose"
                        log_alert["portfolio_idx"] = open_trade
                        self.alerts_log = pd.concat(
                            [
                                self.alerts_log,
                                pd.DataFrame.from_records(log_alert, index=[0]),
                            ],
                            ignore_index=True,
                        )
                        self.save_logs()
                    return

            if order["action"] == "STC":
                str_act = "STC without BTO, maybe alredy sold"
            elif order["action"] == "BTC":
                str_act = "BTC without STO, maybe alredy bought"
            log_alert["action"] = f"{order['action']}-Null-notOpen"
            self.alerts_log = pd.concat(
                [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                ignore_index=True,
            )
            self.save_logs(["alert"])
            print(Back.RED + str_act)
            self.queue_prints.put([str_act, "", "red"])

        elif order["action"] in ["STC", "BTC"]:
            position = self.portfolio.iloc[open_trade]
            if order.get("amnt_left"):
                order, changed = amnt_left(order, position)
                print(
                    Back.GREEN
                    + f"Based on alerted amnt left, Updated order: "
                    + f"xQty: {order['xQty']} and Qty: {order['Qty']}"
                )
                self.queue_prints.put(
                    [
                        f"Based on alerted amnt left, Updated order: "
                        + f"xQty: {order['xQty']} and Qty: {order['Qty']}",
                        "",
                        "green",
                    ]
                )

            # check if position already alerted and closed
            for i in range(1, self.max_stc_orders):
                STC = f"STC{i}"
                # If not alerted, mark it
                if pd.isnull(position[f"{STC}-alerted"]):
                    self.portfolio.loc[open_trade, f"{STC}-alerted"] = 1
                    self.portfolio.loc[open_trade, STC + "-Price-alert"] = order[
                        "price"
                    ]
                    # If alerted and already sold
                    if not pd.isnull(position[f"{STC}-Price"]):
                        for ii in range(i + 1, self.max_stc_orders):
                            STC = f"STC{ii}"
                            if pd.isnull(position[f"{STC}-Price"]):
                                print(Back.GREEN + f"Already sold, but found STC {ii}")
                                self.queue_prints.put(
                                    [f"Already sold, but found STC {ii}", "", "green"]
                                )

                                log_alert["action"] = f"{STC}-DoneBefore"
                                log_alert["portfolio_idx"] = open_trade
                                self.alerts_log = pd.concat(
                                    [
                                        self.alerts_log,
                                        pd.DataFrame.from_records(log_alert, index=[0]),
                                    ],
                                    ignore_index=True,
                                )
                                self.save_logs(["alert"])
                                break

                        if order["xQty"] != 1:  # if partial and sold, leave
                            return
                    break

            else:
                str_STC = f"How many {order['action']} already? looking for empty STC"
                print(Back.RED + str_STC)
                self.queue_prints.put([str_STC, "", "red"])
                for i in range(1, self.max_stc_orders):
                    STC = f"STC{i}"
                    if pd.isnull(position[f"{STC}-Price"]):
                        str_STC = f"Found STC{i}"
                        print(Back.RED + str_STC)
                        self.queue_prints.put([str_STC, "", "red"])
                        break
                else:
                    log_alert["action"] = f"{order['action']}-TooMany"
                    log_alert["portfolio_idx"] = open_trade
                    self.alerts_log = pd.concat(
                        [
                            self.alerts_log,
                            pd.DataFrame.from_records(log_alert, index=[0]),
                        ],
                        ignore_index=True,
                    )
                    self.save_logs(["alert"])
                    return

            qty_bought = position["filledQty"]

            if position["BTO-Status"] in [
                "CANCELED",
                "REJECTED",
                "EXPIRED",
                "CANCEL_REQUESTED",
            ]:
                log_alert["action"] = "Trade-already canceled"
                log_alert["portfolio_idx"] = open_trade
                self.portfolio.iloc[open_trade, "isOpen"] = 0
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                return

            # Close position of STC All or STC SL
            if qty_bought == 0 and order["xQty"] == 1:
                order_id = position["ordID"]
                _ = self.bksession.cancel_order(order_id)

                self.portfolio.loc[open_trade, "isOpen"] = 0

                order_status, _ = self.get_order_info(order_id)
                self.portfolio.loc[open_trade, "BTO-Status"] = order_status.replace(
                    "UROUT", "CANCELED"
                )

                print(
                    Back.GREEN
                    + f"Order Cancelled {order['Symbol']}, closed before fill"
                )
                self.queue_prints.put(
                    [
                        f"Order Cancelled {order['Symbol']}, closed before fill",
                        "",
                        "green",
                    ]
                )

                log_alert["action"] = f"{order['action']}-ClosedBeforeFill"
                log_alert["portfolio_idx"] = open_trade
                self.save_logs()
                return

            # Set STC as exit plan, not bought yet
            elif qty_bought == 0:
                exit_plan = eval(self.portfolio.loc[open_trade, "exit_plan"])
                exit_plan[f"PT{STC[-1]}"] = order["price"]
                self.portfolio.loc[open_trade, "exit_plan"] = str(exit_plan)
                str_msg = f"Exit Plan {order['Symbol']} updated, with PT{STC[-1]}: {order['price']}"
                print(Back.GREEN + str_msg)
                self.queue_prints.put([str_msg, "", "green"])
                log_alert["action"] = f"{order['action']}-partial-BeforeFill-ExUp"
                log_alert["portfolio_idx"] = open_trade
                return

            qty_sold = np.nansum(
                [position[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
            )
            if position["Qty"] - qty_sold == 0:
                self.portfolio.loc[open_trade, "isOpen"] = 0
                print(Back.GREEN + "Already sold")
                self.queue_prints.put(["Already sold", "", "green"])

                log_alert["action"] = f"{STC}-DoneBefore"
                log_alert["portfolio_idx"] = open_trade
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                return

            # # adjust trader qty to match portfolio
            # if not pd.isnull(position['trader_qty']):
            #     qr = qty_bought/position['trader_qty']
            #     order['Qty'] = max(round(order['Qty']/qr), 1)
            # Sell all and close waiting stc orders
            if (
                order["xQty"] == 1 and order["Qty"] is None
            ) or i == self.max_stc_orders:
                if i == self.max_stc_orders and order["xQty"] != 1:
                    print(f"Selling all, max supported STC is {self.max_stc_orders}")
                    self.queue_prints.put(
                        ["Selling all, max supported STC is 3", "", "green"]
                    )
                elif order["xQty"] == 1:
                    print("Selling all, got xQTY =1, check if not true")
                qty_sold = np.nansum(
                    [position[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
                )
                position = self.portfolio.iloc[open_trade]
                order["Qty"] = int(position["Qty"]) - qty_sold

            elif order["xQty"] < 1:  # portion
                order["Qty"] = round(max(qty_bought * order["xQty"], 1))

            if order["Qty"] + qty_sold > qty_bought:
                order["Qty"] = int(qty_bought - qty_sold)
                str_msg = (
                    f"Order {order['Symbol']} Qty exceeded, changed to {order['Qty']}"
                )
                print(Back.RED + Fore.BLACK + str_msg)
                self.queue_prints.put([str_msg, "", "red"])

            # Stop updater to avoid overlapping
            self.update_paused = True
            # close waiting stc orders
            self.close_open_exit_orders(open_trade)
            # remove exits from exit plan
            self.portfolio.loc[open_trade, "exit_plan"] = str(
                {"PT1": None, "PT2": None, "PT3": None, "SL": None}
            )

            order_response, order_id, order, _ = self.confirm_and_send(
                order, pars, self.bksession.make_STC_lim
            )

            log_alert["portfolio_idx"] = open_trade

            if order_response is None:  # Assume trade rejected by user
                log_alert["action"] = f"{order['action']}-notAccepted"
                self.alerts_log = pd.concat(
                    [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                    ignore_index=True,
                )
                self.save_logs(["alert"])
                msg_str = f"{order['action']} not accepted by user, order response null"
                print(Back.GREEN + msg_str)
                self.queue_prints.put([msg_str, "", "green"])
                self.update_paused = False
                return

            order_status, order_info = self.get_order_info(order_id)
            self._add_order_result(
                order["action"],
                order["Symbol"],
                order.get("Qty"),
                order_info.get("filledQuantity") if order_info else 0,
                order_info.get("price") if order_info else order.get("price"),
                order_status,
            )
            self.portfolio.loc[open_trade, STC + "-ordID"] = order_id
            self.portfolio.loc[open_trade, STC + "-Price-actual"] = order[
                "price_actual"
            ]

            ibkr_bad = False
            if order_info is None and self.bksession.name == "ibkr":
                order_status = "FILLED"
                order_info = order
                order_info["quantity"] = order["Qty"]
                order_info["filledQuantity"] = order["Qty"]
                print("IBKR order was None, assuming filled")
                ibkr_bad = True

            # Check if STC price changed
            if order_status in ["FILLED", "EXECUTED", "INDIVIDUAL_FILLS"]:
                self.disc_notifier(order_info)
                self.log_filled_STC(order_id, open_trade, STC)
                if ibkr_bad and self.bksession.name == "ibkr":
                    self.portfolio.loc[open_trade, STC + "-Status"] = order_status
                    self.portfolio.loc[open_trade, STC + "-Qty"] = order_info[
                        "quantity"
                    ]
            else:
                str_STC = f"Submitted: {STC} {order['Symbol']} @{order['price']} Qty:{order['Qty']} ({order['xQty']})"
                print(Back.GREEN + str_STC)
                self.queue_prints.put([str_STC, "", "green"])

            # Log trades_log
            log_alert["action"] = "STC-partial" if order["xQty"] < 1 else "STC-ALL"
            self.alerts_log = pd.concat(
                [self.alerts_log, pd.DataFrame.from_records(log_alert, index=[0])],
                ignore_index=True,
            )
            self.save_logs()
            self.update_paused = False

    def log_filled_STC(self, order_id, open_trade, STC):
        order_status, order_info = self.get_order_info(order_id)
        if order_info.get("orderLegCollection"):
            sold_unts = order_info["orderLegCollection"][0]["quantity"]
        else:
            if order_info["childOrderStrategies"][0]["status"] == "FILLED":
                order_info = order_info["childOrderStrategies"][0]
            elif order_info["childOrderStrategies"][1]["status"] == "FILLED":
                order_info = order_info["childOrderStrategies"][1]
            sold_unts = order_info["orderLegCollection"][0]["quantity"]

        if "price" in order_info.keys():
            stc_price = order_info["price"]
        elif "stopPrice" in order_info.keys():
            stc_price = order_info["stopPrice"]

        bto_price = self.portfolio.loc[open_trade, "Price"]
        bto_price_alert = self.portfolio.loc[open_trade, "Price-alert"]
        bto_price_actual = self.portfolio.loc[open_trade, "Price-actual"]

        if self.portfolio.loc[open_trade, "Type"] == "BTO":
            stc_PnL = float((stc_price - bto_price) / bto_price) * 100
        elif self.portfolio.loc[open_trade, "Type"] == "STO":
            stc_PnL = float((bto_price - stc_price) / bto_price) * 100

        xQty = sold_unts / self.portfolio.loc[open_trade, "Qty"]

        date = order_info["closeTime"]
        # Log portfolio
        self.portfolio.loc[open_trade, STC + "-Status"] = order_status
        self.portfolio.loc[open_trade, STC + "-Price"] = stc_price
        self.portfolio.loc[open_trade, STC + "-Date"] = date
        self.portfolio.loc[open_trade, STC + "-xQty"] = xQty
        self.portfolio.loc[open_trade, STC + "-Qty"] = sold_unts
        self.portfolio.loc[open_trade, STC + "-PnL"] = stc_PnL
        self.portfolio.loc[open_trade, STC + "-ordID"] = order_id

        trade = self.portfolio.loc[open_trade]
        sold_tot = np.nansum(
            [trade[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
        )
        stc_PnL_all = (
            np.nansum(
                [
                    trade[f"STC{i}-PnL"] * trade[f"STC{i}-Qty"]
                    for i in range(1, self.max_stc_orders)
                ]
            )
            / sold_tot
        )
        self.portfolio.loc[open_trade, "PnL"] = stc_PnL_all

        if self.portfolio.loc[open_trade, "Type"] == "BTO":
            stc_PnL_all_alert = (
                np.nansum(
                    [
                        (
                            float(
                                (trade[f"STC{i}-Price-alert"] - bto_price_alert)
                                / bto_price_alert
                            )
                            * 100
                        )
                        * trade[f"STC{i}-Qty"]
                        for i in range(1, self.max_stc_orders)
                    ]
                )
                / sold_tot
            )
            stc_PnL_all_curr = (
                np.nansum(
                    [
                        (
                            float(
                                (trade[f"STC{i}-Price-actual"] - bto_price_actual)
                                / bto_price_actual
                            )
                            * 100
                        )
                        * trade[f"STC{i}-Qty"]
                        for i in range(1, self.max_stc_orders)
                    ]
                )
                / sold_tot
            )
        elif self.portfolio.loc[open_trade, "Type"] == "STO":
            stc_PnL_all_alert = (
                np.nansum(
                    [
                        (
                            float(
                                (bto_price_alert - trade[f"STC{i}-Price-alert"])
                                / bto_price_alert
                            )
                            * 100
                        )
                        * trade[f"STC{i}-Qty"]
                        for i in range(1, self.max_stc_orders)
                    ]
                )
                / sold_tot
            )
            stc_PnL_all_curr = (
                np.nansum(
                    [
                        (
                            float(
                                (bto_price_actual - trade[f"STC{i}-Price-actual"])
                                / bto_price_actual
                            )
                            * 100
                        )
                        * trade[f"STC{i}-Qty"]
                        for i in range(1, self.max_stc_orders)
                    ]
                )
                / sold_tot
            )

        self.portfolio.loc[open_trade, "PnL-alert"] = stc_PnL_all_alert
        self.portfolio.loc[open_trade, "PnL-actual"] = stc_PnL_all_curr

        mutipl = 1 if trade["Asset"] == "option" else 0.01  # pnl already in %

        self.portfolio.loc[open_trade, "PnL$"] = (
            stc_PnL_all * bto_price * mutipl * sold_tot
        )
        self.portfolio.loc[open_trade, "PnL$-alert"] = (
            stc_PnL_all_alert * bto_price_alert * mutipl * sold_tot
        )
        self.portfolio.loc[open_trade, "PnL$-actual"] = (
            stc_PnL_all_curr * bto_price_actual * mutipl * sold_tot
        )

        symb = self.portfolio.loc[open_trade, "Symbol"]

        sold_Qty = self.portfolio.loc[
            open_trade, [f"STC{i}-Qty" for i in range(1, self.max_stc_orders)]
        ].sum()

        str_STC = (
            f"{STC} {symb} @{stc_price} Qty:"
            + f"{sold_unts}({int(xQty*100)}%), for {stc_PnL:.2f}%"
        )

        if sold_Qty == self.portfolio.loc[open_trade, "Qty"]:
            str_STC += " (Closed)"
            self.portfolio.loc[open_trade, "isOpen"] = 0

        self._add_order_result(
            f"{STC}-FILLED", symb, sold_unts, sold_unts, stc_price, "FILLED"
        )
        print(Back.GREEN + f"Filled: {str_STC}")
        self.queue_prints.put([f"Filled: {str_STC}", "", "green"])
        self.save_logs()

    def exit_percent_to_price(self, open_trade):
        trade = self.portfolio.loc[open_trade]
        if trade["BTO-Status"] not in ["FILLED", "EXECUTED"]:
            return

        price = trade["Price"]
        exit_plan = eval(self.portfolio.loc[open_trade, "exit_plan"])
        exit_plan_o = exit_plan.copy()

        for exit in [f"PT{i}" for i in range(1, self.max_stc_orders)]:
            if (
                exit_plan.get(exit) is None
                or not isinstance(exit_plan[exit], str)
                or "%" not in exit_plan[exit]
            ):
                continue

            if "TS" in exit_plan[exit]:
                pt, ts = exit_plan[exit].split("TS")
                if "%" in pt:  # format val%TSval%
                    if trade["Type"] == "STO":
                        print(
                            "\033[91mWARNING: TrailingStop in buy to close. Why? \033[0m"
                        )
                        if "%" in pt:
                            ptv = round(
                                price * (1 - float(pt.replace("%", "")) / 100), 2
                            )
                    else:
                        ptv = round(price * (1 + float(pt.replace("%", "")) / 100), 2)
                else:  # format valTSval%
                    ptv = float(pt)
                if "%" in ts:  # format TSval%
                    ts = round(price * (float(ts.replace("%", "")) / 100), 2)
                else:  # format TSval
                    ts = float(ts)
                exit_plan[exit] = f"{ptv}TS{ts}"
            else:  # format val%
                if "%" in exit_plan[exit]:
                    if trade["Type"] == "STO":
                        ptv = round(
                            price * (1 - float(exit_plan[exit].replace("%", "")) / 100),
                            2,
                        )
                    else:
                        ptv = round(
                            price * (1 + float(exit_plan[exit].replace("%", "")) / 100),
                            2,
                        )
                else:
                    ptv = float(exit_plan[exit])
                exit_plan[exit] = ptv

        sl = exit_plan["SL"]
        if sl is not None and isinstance(sl, str) and "%" in sl:
            if "TS" in sl:  # format TSval%
                sl = round(
                    price * (float(sl.replace("%", "").replace("TS", "")) / 100), 2
                )
                exit_plan["SL"] = f"TS{sl}"
            else:  # format val%
                if trade["Type"] == "STO":
                    exit_plan["SL"] = round(
                        price * (1 + float(sl.replace("%", "")) / 100), 2
                    )
                else:
                    exit_plan["SL"] = round(
                        price * (1 - float(sl.replace("%", "")) / 100), 2
                    )

        self.portfolio.loc[open_trade, "exit_plan"] = str(exit_plan)

        if exit_plan_o != exit_plan:
            str_msg = (
                f"Updated exits for from % to value, from:{exit_plan_o}, to:{exit_plan}"
            )
            print(Back.GREEN + str_msg)
            self.queue_prints.put([str_msg, "", "green"])

    def update_orders(self):
        for i in range(len(self.portfolio)):
            if self.update_paused:
                return
            self.close_expired(i)
            trade = self.portfolio.iloc[i]
            redo_orders = False

            if trade["isOpen"] == 0:
                continue

            # check if inverse TSbuy stop has reached or update stop
            if trade["BTO-Status"] == "invTSbuy":
                self.order_update_rate = 1
                ts_const, max_price = trade["open_trailingstop"].split(",")
                max_price = eval(max_price.split(":")[1])
                ts_const = eval(ts_const.split(":")[1])
                stp_price = max_price - ts_const
                quote_opt = self.price_now(trade["Symbol"], "STC", 1)
                if quote_opt == -1:
                    continue
                if quote_opt <= stp_price * 1.01:  # 1%
                    if pd.isna(trade["trader_qty"]):
                        qty = 1
                    else:
                        qty = int(trade["trader_qty"])
                    order = {
                        "Symbol": trade["Symbol"],
                        "action": "BTO",
                        "asset": trade["Asset"],
                        "price": quote_opt,
                        "Qty": qty,
                    }
                    order = self.round_order_price(order, trade)
                    pars = (
                        f"BTO {trade['trader_qty']} {trade['Symbol']} @{order['price']}"
                    )
                    order_response, order_id, order, _ = self.confirm_and_send(
                        order, pars, self.bksession.make_BTO_lim_order
                    )

                    if order_response is None:  # trade not successful
                        str_msg = "BTO after invTS did not go through, order response is none. Will try again next update. Trigger an STC to cancel it"
                        print(Back.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "green"])
                        return
                    order_status, order_info = self.get_order_info(order_id)
                    self.portfolio.loc[i, "Qty"] = order_info["quantity"]
                    self.portfolio.loc[i, "filledQty"] = order_info["filledQuantity"]
                    self.portfolio.loc[i, "BTO-Status"] = order_info["status"]
                    self.portfolio.loc[i, "Price"] = order_info.get("price")
                    self.portfolio.loc[i, "trader_qty"] = order_info.get("trader_qty")
                    self.portfolio.loc[i, "ordID"] = order_id
                    self.order_update_rate = 10
                else:
                    if quote_opt > max_price:
                        max_price = quote_opt
                        self.portfolio.loc[
                            i, "open_trailingstop"
                        ] = f"ts:{ts_const},max_price:{max_price}"
                        self.portfolio.loc[i, "Price"] = stp_price
                self.save_logs("port")
                continue

            if trade["BTO-Status"] not in ["FILLED", "CANCELED", "REJECTED"]:
                # if str and has comma, take first
                if isinstance(trade["ordID"], str):
                    ordID = trade["ordID"].split(",")[0]
                else:
                    ordID = trade["ordID"]
                order_status, order_info = self.get_order_info(ordID)
                if order_status is None:
                    print(
                        Back.GREEN
                        + f"Order info not found for {trade['Symbol']} ordid {ordID}"
                    )
                    continue

                if order_status == "REJECTED":
                    self.portfolio.loc[i, "BTO-Status"] = order_status
                    self.portfolio.loc[i, "isOpen"] = 0

                    str_msg = (
                        f"BTO {self.portfolio.loc[i, 'Symbol']} Status: {order_status}"
                    )
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    continue
                elif order_status == "MISSING":
                    self.portfolio.loc[i, "BTO-Status"] = order_status
                    self.portfolio.loc[i, "isOpen"] = 0
                    str_msg = (
                        f"BTO {trade['Symbol']} order ID not found, probably canceled"
                    )
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    continue
                # Check if number filled Qty changed
                qty_fill = order_info["filledQuantity"]
                qty_fill_old = self.portfolio.loc[i, "filledQty"]
                # If so, redo orders
                if (
                    not (pd.isnull(qty_fill_old) or qty_fill_old == 0)
                    and qty_fill_old != qty_fill
                ):
                    redo_orders = True

                if order_status in ["FILLED", "EXECUTED"]:
                    price = order_info.get("price")
                    self.portfolio.loc[i, "Price"] = price
                    self.disc_notifier(order_info)
                    str_msg = f"ENTERED {order_info['orderLegCollection'][0]['instrument']['symbol']} filled @ {price}. Status: {order_status}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    self._add_order_result(
                        "ENTERED",
                        order_info["orderLegCollection"][0]["instrument"]["symbol"],
                        order_info.get("quantity"),
                        order_info.get("filledQuantity"),
                        price,
                        order_status,
                    )

                    # Add default short exits, once filled and price is known
                    if self.portfolio.loc[i, "Type"] == "STO":
                        exit_plan = eval(self.portfolio.loc[i, "exit_plan"])
                        if (
                            len(self.cfg["shorting"]["BTC_PT"])
                            and exit_plan.get("PT1") is None
                        ):
                            exit_plan["PT1"] = round(
                                price
                                * (1 - float(self.cfg["shorting"]["BTC_PT"]) / 100),
                                2,
                            )
                        if (
                            len(self.cfg["shorting"]["BTC_SL"])
                            and exit_plan.get("SL") is None
                        ):
                            exit_plan["SL"] = round(
                                price
                                * (1 + float(self.cfg["shorting"]["BTC_SL"]) / 100),
                                2,
                            )
                        self.portfolio.loc[i, "exit_plan"] = str(exit_plan)

                    # Update exits from % to value
                    self.portfolio.loc[i, "BTO-Status"] = order_info["status"]
                    self.exit_percent_to_price(i)

                self.portfolio.loc[i, "filledQty"] = order_info["filledQuantity"]

                if order_status == "REJECTED":
                    self.portfolio.loc[i, "isOpen"] = 0
                    str_msg = f"BTO {order_info['orderLegCollection'][0]['instrument']['symbol']} Status: {order_status}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                elif (
                    order_status
                    in [
                        "QUEUED",
                        "WORKING",
                        "OPEN",
                        "AWAITING_CONDITION",
                        "PENDING_ACTIVATION",
                        "AWAITING_MANUAL_REVIEW",
                        "PARTIAL",
                    ]
                    and self.portfolio.loc[i, "Type"] == "BTO"
                ):
                    # see if too much time has passed
                    bot_time = datetime.strptime(
                        self.portfolio.loc[i, "Date"], "%Y-%m-%d %H:%M:%S.%f"
                    )
                    time_difference = (datetime.now() - bot_time).total_seconds()
                    if cfg["order_configs"]["kill_if_nofill"] not in ["0", ""]:
                        max_time = int(cfg["order_configs"]["kill_if_nofill"])
                        if time_difference > max_time:
                            order_status = self.bksession.cancel_order(
                                order_info["order_id"]
                            )
                            str_msg = f"Killing order after not filling in {max_time} secs [{time_difference}] {order_info['orderLegCollection'][0]['instrument']['symbol']} Status: {order_status}"
                            print(Back.GREEN + str_msg)
                            self.queue_prints.put([str_msg, "", "green"])

                    kill_price_diff = cfg["order_configs"].get("kill_if_price_diff", "0")
                    if kill_price_diff not in ["0", ""]:
                        max_diff = float(kill_price_diff)
                        now_utc = datetime.utcnow()
                        now_et_hour = (now_utc.hour - 5) % 24
                        now_et_minute = now_utc.minute
                        if now_et_hour == 15 and now_et_minute >= 50:
                            alert_price = self.portfolio.loc[i, "Price"]
                            curr_quote = self.price_now(trade["Symbol"], "BTO", 1)
                            if curr_quote > 0 and alert_price > 0:
                                pct_diff = abs(curr_quote - alert_price) / alert_price * 100
                                if pct_diff > max_diff:
                                    order_status = self.bksession.cancel_order(
                                        order_info["order_id"]
                                    )
                                    str_msg = f"EOD cancel: {trade['Symbol']} price {curr_quote} differs {pct_diff:.1f}% from alert {alert_price} (>{max_diff}%) Status: {order_status}"
                                    print(Back.YELLOW + str_msg)
                                    self.queue_prints.put([str_msg, "", "yellow"])
                trade = self.portfolio.iloc[i]
                self.save_logs("port")

            if pd.isnull(trade["filledQty"]) or trade["filledQty"] == 0:
                continue

            if not pd.isnull(trade.get("BTO-avg-Status")) and trade.get(
                "BTO-avg-Status"
            ) not in ["FILLED", "CANCELED", "REJECTED", "MISSING"]:
                if isinstance(trade["ordID"], str):
                    ordID = trade["ordID"].split(",")[-1]
                else:
                    ordID = trade["ordID"]
                order_status, order_info = self.get_order_info(ordID)
                if order_info is None:
                    continue
                if order_status == "MISSING":
                    self.portfolio.loc[i, "BTO-avg-Status"] = "MISSING"
                    str_msg = f"BTO-avg {trade['Symbol']} order ID not found, probably canceled"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    continue

                if order_info["status"] in ["FILLED", "EXECUTED"]:
                    or_price = (
                        self.portfolio.loc[i, "Price"]
                        * self.portfolio.loc[i, "filledQty"]
                    )
                    nw_price = order_info["price"] * order_info["filledQuantity"]
                    avg_price = round(
                        (or_price + nw_price)
                        / (
                            self.portfolio.loc[i, "filledQty"]
                            + order_info["filledQuantity"]
                        ),
                        2,
                    )
                    self.portfolio.loc[i, "Price"] = avg_price

                    self.portfolio.loc[i, "filledQty"] += order_info["filledQuantity"]
                    self.disc_notifier(order_info)
                    self.close_open_exit_orders(i)

                    self.portfolio.loc[i, "BTO-avg-Status"] = order_info["status"]

                    redo_orders = True
                    trade = self.portfolio.iloc[i]
                    self.save_logs("port")

                    str_msg = f"BTO-avg {order_info['orderLegCollection'][0]['instrument']['symbol']} executed @ {order_info['price']}. Status: {order_status}"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    self.disc_notifier(order_info)

                    # Update exits from % to value
                    self.exit_percent_to_price(i)

            # For shorting positions if closing end of day
            if trade["Type"] == "STO" and self.cfg["shorting"].getboolean("BTC_EOD"):
                time_now = datetime.now().time()
                time_closed = datetime.strptime(
                    self.cfg["general"]["off_hours"].split(",")[0], "%H"
                )
                time_quarter = time_closed - timedelta(minutes=15)
                time_five = time_closed - timedelta(minutes=5)
                # Change exits before 15 min to close
                if (
                    time_now >= time_quarter.time()
                    and time_now < time_five.time()
                    and len(self.cfg["shorting"]["BTC_EOD_PT_SL"])
                ):
                    exit_plan = eval(trade["exit_plan"])
                    PT, SL = self.cfg["shorting"]["BTC_EOD_PT_SL"].split(",")
                    SL, PT = eval(SL) / 100, eval(PT) / 100

                    if self.EOD.get(trade["Symbol"]) != "15min":
                        # Close and send lim order
                        self.close_open_exit_orders(i)
                        quote = self.price_now(trade["Symbol"], "BTC", 1)
                        # get the STC number to save PT
                        STC = f"STC{self.max_stc_orders-1}"
                        for ith in range(1, self.max_stc_orders):
                            STC = f"STC{ith}"
                            if pd.isnull(trade[STC + "-ordID"]):
                                break
                        exit_plan = {
                            "PT1": None,
                            "PT2": None,
                            "PT3": None,
                            "SL": round(quote + SL * quote, 2),
                        }
                        exit_plan[f"PT{ith}"] = round(quote - PT * quote, 2)

                        self.portfolio.at[i, "exit_plan"] = str(exit_plan)
                        redo_orders = True
                        self.exit_percent_to_price(i)
                        str_msg = f'updating exits option {trade["Symbol"]} 15 min before EOD with {int(SL*100)}% SL and {int(PT*100)}% PT'
                        print(Back.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "green"])
                        self.EOD[trade["Symbol"]] = "15min"

                # Close position 5 min to close
                elif (
                    time_now >= time_five.time()
                    and time_now < time_closed.time()
                    and self.EOD.get(trade["Symbol"]) != "5min"
                ):
                    print(f'closing option {trade["Symbol"]} 5 min before EOD')
                    quote = self.price_now(trade["Symbol"], "BTC", 1)

                    # Close and send lim order
                    self.close_open_exit_orders(i)
                    order = {}
                    order["action"] = "BTC"
                    order["Symbol"] = trade["Symbol"]
                    qty_sold = np.nansum(
                        [trade[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
                    )
                    order["Qty"] = int(int(trade["filledQty"]) - qty_sold)
                    order["price"] = quote
                    self.update_paused = True
                    _, order_id, order, _ = self.confirm_and_send(
                        order, f'EOD {order["Symbol"]}', self.bksession.make_STC_lim
                    )

                    # add order id
                    for ith in range(1, self.max_stc_orders):
                        STC = f"STC{ith}"
                        if pd.isnull(trade[STC + "-ordID"]):
                            break
                    self.portfolio.loc[i, STC + "-ordID"] = order_id
                    self.EOD[trade["Symbol"]] = "5min"
                    self.save_logs("port")
                    self.update_paused = False
            if self.update_paused:
                return
            if trade["Type"] == "STO" and (
                cfg["shorting"]["avg_down"] is not None
                or (
                    not pd.isna(trade.get("avg_down"))
                    and not pd.isna(eval(trade["avg_down"]).get("avgs"))
                )
            ):
                price_currernt = self.price_now(trade["Symbol"], "STO", 1)
                avg_down = {}
                if not pd.isna(trade.get("avg_down")):
                    avg_down = eval(trade["avg_down"])

                avg_down_list = eval(cfg["shorting"]["avg_down"])
                if not pd.isna(avg_down.get("avgs")):
                    avg_down_list = avg_down.get("avgs")

                for avg in avg_down_list:
                    avg_rat, avg_ratio = avg
                    if avg_down.get(str(avg_rat)) == "sent":
                        continue
                    price_ori = trade.get("original_price")
                    price_ori = price_ori if not pd.isna(price_ori) else trade["Price"]
                    qty_ori = trade.get("original_qty")
                    qty_ori = qty_ori if not pd.isna(qty_ori) else trade["Qty"]
                    avg_price = price_ori * avg_rat
                    if (
                        avg_price < price_currernt
                        and avg_down.get(str(avg_rat)) is None
                    ):
                        if self.update_paused:
                            return
                        order = {}
                        order["action"] = "STO"
                        order["Symbol"] = trade["Symbol"]
                        order["Qty"] = max(1, int(qty_ori * avg_ratio))
                        order["price"] = price_currernt
                        order_response, order_id = self.bksession.send_order(
                            self.bksession.make_BTO_lim_order(**order)
                        )
                        order_status, order_info = self.get_order_info(order_id)

                        if order_response is None:  # Assume trade not accepted
                            print(Back.GREEN + "BTO avg not accepted by user")
                            self.queue_prints.put(
                                ["BTO avg not accepted by user", "", "green"]
                            )
                            continue
                        elif order_status == "REJECTED":
                            print(
                                Back.GREEN
                                + f"STO avg down {trade['Symbol']} Status: {order_status}"
                            )
                            self.queue_prints.put(
                                [
                                    f"STO avg down {trade['Symbol']} Status: {order_status}",
                                    "",
                                    "green",
                                ]
                            )
                            continue

                        self.portfolio.loc[i, "BTO-avg-Status"] = order_status
                        # if np.integer or int, turn to str
                        if isinstance(
                            self.portfolio.loc[i, "ordID"], (np.integer, float)
                        ):
                            self.portfolio.loc[i, "ordID"] = str(
                                self.portfolio.loc[i, "ordID"]
                            )
                        self.portfolio.loc[i, "ordID"] += f",{order_id}"

                        if pd.isnull(self.portfolio.loc[i, "Avged"]):
                            self.portfolio.loc[i, "Avged"] = 1
                            self.portfolio.loc[i, "Avged-prices-alert"] = order["price"]
                            self.portfolio.loc[i, "Avged-prices"] = order_info["price"]
                            self.portfolio.loc[i, "Avged-Qty"] = order_info["quantity"]
                        else:
                            self.portfolio.loc[i, "Avged"] += 1
                            al_pr = self.portfolio.loc[i, "Avged-prices-alert"]
                            av_pr = self.portfolio.loc[i, "Avged-prices"]
                            av_qt = self.portfolio.loc[i, "Avged-Qty"]
                            self.portfolio.loc[
                                i, "Avged-prices-alert"
                            ] = f"{al_pr},{order['price']}"
                            self.portfolio.loc[
                                i, "Avged-prices"
                            ] = f"{av_pr},{order_info['price']}"
                            self.portfolio.loc[
                                i, "Avged-Qty"
                            ] = f"{av_qt},{order_info['quantity']}"

                        avg = self.portfolio.loc[i, "Avged"]

                        self.portfolio.loc[i, "Qty"] += order_info["quantity"]
                        if order_status in ["FILLED", "EXECUTED"]:
                            or_price = (
                                self.portfolio.loc[i, "Price"]
                                * self.portfolio.loc[i, "filledQty"]
                            )
                            nw_price = (
                                order_info["price"] * order_info["filledQuantity"]
                            )
                            avg_price = round(
                                (or_price + nw_price)
                                / (
                                    self.portfolio.loc[i, "filledQty"]
                                    + order_info["filledQuantity"]
                                ),
                                2,
                            )
                            self.portfolio.loc[i, "Price"] = avg_price

                            self.portfolio.loc[i, "filledQty"] += order_info[
                                "filledQuantity"
                            ]
                            self.disc_notifier(order_info)
                            self.close_open_exit_orders(i)
                        str_msg = f"BTO {avg} th AVG DOWN, {order['Symbol']} sent @{order_info['price']}. Status: {order_status}"
                        print(Back.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "green"])

                        self.portfolio.loc[i, "BTO-avg-Status"] = order_status

                        avg_down[str(avg_rat)] = "sent"
                        self.portfolio.loc[i, "avg_down"] = str(avg_down)

            if redo_orders:
                self.close_open_exit_orders(i)
            self.exit_percent_to_price(i)
            trade = self.portfolio.iloc[i]
            exit_plan = eval(trade["exit_plan"])
            if exit_plan != {}:
                self.make_exit_orders(i, exit_plan)
                self.exit_percent_to_price(i)

            # Go over STC orders and check status
            for ii in range(1, self.max_stc_orders):
                if self.update_paused:
                    return
                STC = f"STC{ii}"
                trade = self.portfolio.iloc[i]
                STC_ordID = trade.get(STC + "-ordID")

                if pd.isnull(STC_ordID) or trade[STC + "-Status"] in [
                    "FILLED",
                    "REJECTED",
                ]:
                    continue

                # Get status exit orders
                if isinstance(STC_ordID, str) and STC_ordID.isdigit():
                    STC_ordID = int(float(STC_ordID))  # Might be read as a float

                order_status, order_info = self.get_order_info(STC_ordID)
                if order_status is None:
                    continue

                if (
                    order_status == "CANCELED"
                    and self.bksession.name == "tda"
                    and order_info["orderStrategyType"] == "OCO"
                ):
                    # Try next order number. OCO gets canceled when one of child ordergets filled.
                    # This is for TDA OCO
                    STC_ordID = int(STC_ordID)
                    order_status, order_info = self.get_order_info(int(STC_ordID) + 1)
                    if order_status == "FILLED":
                        STC_ordID = STC_ordID + 1
                        self.portfolio.loc[i, STC + "-ordID"] = STC_ordID
                    else:  # try the other one for tda
                        order_status, order_info = self.get_order_info(STC_ordID + 2)
                        if order_status == "FILLED":
                            STC_ordID = STC_ordID + 2
                            self.portfolio.loc[i, STC + "-ordID"] = STC_ordID

                elif "PARTIAL" in order_status:
                    # check if partial has changed since last time
                    if (
                        order_info["filledQuantity"]
                        == self.portfolio.loc[i, STC + "-Qty"]
                    ):
                        continue
                    # Update filled qty
                    self.portfolio.loc[i, STC + "-Qty"] = order_info["filledQuantity"]
                    self.log_filled_STC(STC_ordID, i, STC)
                    self.portfolio.loc[i, STC + "-xQty"] = np.nan

                self.portfolio.loc[i, STC + "-Status"] = order_status
                trade = self.portfolio.iloc[i]

                if order_status in ["FILLED", "EXECUTED"] and np.isnan(
                    trade[STC + "-xQty"]
                ):
                    self.log_filled_STC(STC_ordID, i, STC)
                    self.disc_notifier(order_info)

            self.check_dynamic_pt(i)

        self.save_logs("port")

    def SL_below_market(self, order, new_SL_ratio=0.95):
        SL = order.get("SL")
        if order["action"] == "STC":
            price_now = self.price_now(order["Symbol"], "STC", 1)

            if SL > price_now:
                new_SL = round(price_now * new_SL_ratio, 2)
                print(
                    Back.RED
                    + f"{order['Symbol']} SL below bid price, changed from {SL} to {new_SL}"
                )
                self.queue_prints.put(
                    [
                        f"{order['Symbol']} SL below bid price, changed from {SL} to {new_SL}",
                        "",
                        "red",
                    ]
                )
                order["SL"] = new_SL
            return order

    def make_exit_orders(self, open_trade, exit_plan):
        i = open_trade
        trade = self.portfolio.iloc[i]

        # Calculate x/Qty:
        Qty_bought = trade["filledQty"]
        nPTs = len(
            [
                i
                for i in range(1, self.max_stc_orders)
                if exit_plan.get(f"PT{i}") is not None
            ]
        )
        if nPTs != 0:
            Qty = [round(Qty_bought / nPTs)] * nPTs
            Qty[-1] = int(Qty_bought - sum(Qty[:-1]))

            xQty = [round(1 / nPTs, 1)] * nPTs
            xQty[-1] = 1 - sum(xQty[:-1])

        # Go over exit plans and make orders
        order = {"Symbol": trade["Symbol"]}
        for ii in range(1, nPTs + 1):
            STC = f"STC{ii}"

            if trade[STC + "-Status"] in ["FILLED", "EXECUTED"]:
                continue
            # If Option add strike field
            ord_inf = trade["Symbol"].split("_")
            if len(ord_inf) == 2:
                opt_type = "C" if "C" in ord_inf[1] else "P"
                strike = str(re.split("C|P", ord_inf[1])[1]) + opt_type
                order["strike"] = strike

            STC_ordID = trade[STC + "-ordID"]
            if not pd.isnull(STC_ordID):
                # assume PT with trailing stop at lim has SL, TS can be 0
                if (
                    isinstance(exit_plan[f"PT{ii}"], str)
                    and "TS" in exit_plan[f"PT{ii}"]
                ):
                    self.order_update_rate = 1
                    trigger = float(exit_plan[f"PT{ii}"].split("TS")[0])
                    TS = eval(exit_plan[f"PT{ii}"].split("TS")[1])
                    quote_opt = self.price_now(trade["Symbol"], "STC", 1)
                    if quote_opt >= trigger:
                        self.close_open_exit_orders(open_trade)
                        order = {"Symbol": trade["Symbol"]}
                        if TS > 0:
                            ord_func = self.bksession.make_STC_SL_trailstop
                            order = self.calculate_stoploss(order, trade, TS)
                        else:
                            ord_func = self.bksession.make_STC_lim
                            order["price"] = quote_opt
                        order = self.round_order_price(order, trade)
                        order["Qty"] = int(trade["Qty"])
                        order["xQty"] = 1
                        order["action"] = (
                            trade["Type"].replace("BTO", "STC").replace("STO", "BTC")
                        )

                        _, STC_ordID = self.bksession.send_order(ord_func(**order))
                        if STC_ordID is None:
                            print("Sent order got None", order)
                            continue

                        if order.get("price"):
                            str_prt = f"{STC} {order['Symbol']} @{order['price']}(Qty:{order['Qty']}) sent during order update"
                        else:
                            str_prt = f"{STC} {order['Symbol']} TS @{order.get('trail_stop_const')} (Qty:{order['Qty']}) sent during order update"
                        exit_plan[f"PT{ii}"] = TS if TS > 0 else quote_opt
                        self.portfolio.loc[i, "exit_plan"] = str(exit_plan)
                        print(Back.GREEN + str_prt)
                        self.queue_prints.put([str_prt, "", "green"])
                        self.portfolio.loc[i, STC + "-ordID"] = STC_ordID
                        trade = self.portfolio.iloc[i]
                        self.save_logs("port")
                        self.order_update_rate = 10

                # Adjust if necessary Qty based on remaining shares
                if nPTs > 1:
                    ord_stat, ord_inf = self.get_order_info(STC_ordID)
                    iord_qty = ord_inf.get("quantity")
                    if iord_qty is None:
                        iord_qty = ord_inf["childOrderStrategies"][0]["quantity"]
                    if Qty[ii - 1] != iord_qty:
                        Qty[ii - 1] = int(iord_qty)
                        uleft = Qty_bought - sum(Qty[:ii])
                        if uleft == 0:
                            break
                        nPts = len(Qty) - ii
                        if nPts != 0:
                            Qty = Qty[:ii] + [round(uleft / nPts)] * nPts
                            Qty[-1] = int(Qty_bought - sum(Qty[:-1]))
                        else:
                            Qty[-1] = int(uleft)
                        xQty = [round(u / Qty_bought, 1) for u in Qty]

            else:
                SL = exit_plan["SL"]
                # Check if exit prices are strings (stock price for option)
                if isinstance(SL, str) and "TS" not in SL:
                    SL = None
                if isinstance(exit_plan[f"PT{ii}"], str):
                    exit_plan[f"PT{ii}"] = None

                ord_func = None
                # Lim and Sl OCO order
                if exit_plan[f"PT{ii}"] is not None and SL is not None:
                    # Lim_SL order
                    ord_func = self.bksession.make_Lim_SL_order
                    order["PT"] = exit_plan[f"PT{ii}"]
                    order["SL"] = exit_plan["SL"]
                    order["Qty"] = Qty[ii - 1]
                    order["xQty"] = xQty[ii - 1]
                    order["action"] = (
                        trade["Type"].replace("STO", "BTC").replace("BTO", "STC")
                    )
                    if self.bksession.name not in ["tda", "ts", "cobra"]:
                        str_prt = f"WARNING: {self.bksession.name} does not support OCO orders. Only the PT will be sent without SL. For OCO pass a PT as a string with 0%TS (e.g. 50%TS0%) and SL."
                        print(Back.RED + str_prt)
                        self.queue_prints.put([str_prt, "", "red"])
                # Lim order
                elif exit_plan[f"PT{ii}"] is not None and SL is None:
                    ord_func = self.bksession.make_STC_lim
                    order["price"] = exit_plan[f"PT{ii}"]
                    order["Qty"] = Qty[ii - 1]
                    order["xQty"] = xQty[ii - 1]
                    order["action"] = (
                        trade["Type"].replace("STO", "BTC").replace("BTO", "STC")
                    )

                # SL order
                elif ii == 1 and SL is not None:
                    if isinstance(SL, str) and "TS" in SL:
                        ord_func = self.bksession.make_STC_SL_trailstop
                        order = self.calculate_stoploss(order, trade, exit_plan["SL"])
                    else:
                        ord_func = self.bksession.make_STC_SL
                        order["SL"] = exit_plan["SL"]

                    order["Qty"] = int(trade["Qty"])
                    order["xQty"] = 1
                    order["action"] = (
                        trade["Type"].replace("STO", "BTC").replace("BTO", "STC")
                    )

                elif ii > 1 and SL is not None:
                    break

                else:  # SL is None because still a %
                    break

                # Check that is below actual price
                if trade["Type"] == "BTO":
                    if order.get("SL") is not None and isinstance(
                        order.get("SL"), (int, float)
                    ):
                        order["action"] = (
                            trade["Type"].replace("STO", "BTC").replace("BTO", "STC")
                        )
                        order = self.SL_below_market(order)
                        order = self.round_order_price(order, trade)

                if ord_func is not None and order["Qty"] > 0:
                    order = self.round_order_price(order, trade)
                    _, STC_ordID = self.bksession.send_order(ord_func(**order))
                    if STC_ordID is None:
                        print("Sent order got None", order)
                        continue
                    if order.get("price"):
                        str_prt = f"{STC} {order['Symbol']} @{order['price']}(Qty:{order['Qty']}) sent during order update"
                    else:
                        str_prt = f"{STC} {order['Symbol']} @PT:{order.get('PT')}/SL:{order.get('SL')} (Qty:{order['Qty']}) sent during order update"
                    print(Back.GREEN + str_prt)
                    self.queue_prints.put([str_prt, "", "green"])
                    self.portfolio.loc[i, STC + "-ordID"] = STC_ordID
                    trade = self.portfolio.iloc[i]
                    self.save_logs("port")
                else:
                    break
        # no PTs but trailing stop
        if nPTs == 0 and exit_plan["SL"] is not None and pd.isnull(trade["STC1-ordID"]):
            SL = exit_plan["SL"]

            order["Qty"] = int(trade["Qty"])
            order["xQty"] = 1
            order["action"] = trade["Type"].replace("STO", "BTC").replace("BTO", "STC")

            if isinstance(SL, str) and "TS" in SL:
                ord_func = self.bksession.make_STC_SL_trailstop
                order = self.calculate_stoploss(order, trade, exit_plan["SL"])
                msg = f"Trailing stop of {exit_plan['SL']} constant % sent during order update"
            else:
                ord_func = self.bksession.make_STC_SL
                order["SL"] = exit_plan["SL"]
                order = self.round_order_price(order, trade)
                msg = f"SL of {exit_plan['SL']} constant % sent during order update"

            try:
                _, STC_ordID = self.bksession.send_order(ord_func(**order))
                if STC_ordID is None:
                    print("Sent order got None", order)
                    return
                str_prt = f"STC1 {order['Symbol']} {msg}"
                print(Back.GREEN + str_prt)
                self.queue_prints.put([str_prt, "", "green"])
                self.portfolio.loc[i, "STC1-ordID"] = STC_ordID
                trade = self.portfolio.iloc[i]
                self.save_logs("port")
            except Exception as e:
                str_prt = "error in making SL exit order " + str(e)
                print(Back.RED + str_prt)
                self.queue_prints.put([str_prt, "", "red"])

    def check_dynamic_pt(self, i):
        import datetime
        now_utc = datetime.datetime.utcnow()
        cur_min = now_utc.minute
        last_min = self._dynamic_pt_last_minute.get(i, -1)
        if cur_min == last_min:
            return
        self._dynamic_pt_last_minute[i] = cur_min
        trade = self.portfolio.iloc[i]
        if not self.cfg["order_configs"].getboolean("dynamic_pt_enabled"):
            return
        if trade.get("Type") != "BTO":
            return
        if trade.get("BTO-Status") not in ["FILLED", "EXECUTED"]:
            return
        if pd.isna(trade.get("filledQty")) or trade["filledQty"] == 0:
            return
        exit_plan = eval(trade["exit_plan"])
        if any(exit_plan.get(k) is not None for k in ["PT1", "PT2", "PT3", "SL"]):
            return
        if "dynamic_pt_done" not in self.portfolio.columns:
            self.portfolio["dynamic_pt_done"] = ""
        levels = eval(self.cfg["order_configs"]["dynamic_pt_levels"])
        sizes = eval(self.cfg["order_configs"]["dynamic_pt_sizes"])
        sl_pct = (
            float(self.cfg["order_configs"]["dynamic_pt_sl"])
            if self.cfg["order_configs"]["dynamic_pt_sl"]
            else 0
        )
        do_breakeven = self.cfg["order_configs"].getboolean("dynamic_pt_breakeven")
        if len(levels) != len(sizes):
            return
        done_val = trade.get("dynamic_pt_done", np.nan)
        if pd.isna(done_val) or done_val == "":
            done_levels = []
        else:
            done_levels = [int(x) for x in str(done_val).split(",") if x]
        quote = self.price_now(trade["Symbol"], "STC", 1)
        if quote <= 0:
            return
        entry_price = trade["Price"]
        total_qty = int(trade["filledQty"])
        pnl_pct = (quote - entry_price) / entry_price * 100

        def _calc_remaining():
            qty_sold = 0
            for si in range(1, self.max_stc_orders):
                sq = self.portfolio.loc[i].get(f"STC{si}-Qty")
                if not pd.isna(sq):
                    qty_sold += int(sq)
            return int(trade["filledQty"]) - qty_sold

        def _find_slot():
            for ith in range(1, self.max_stc_orders):
                oid = self.portfolio.loc[i].get(f"STC{ith}-ordID")
                stat = self.portfolio.loc[i].get(f"STC{ith}-Status")
                if pd.isna(oid) or oid == "":
                    return f"STC{ith}"
            for ith in range(1, self.max_stc_orders):
                stat = self.portfolio.loc[i].get(f"STC{ith}-Status")
                if stat in ["FILLED", "EXECUTED", "CANCELED", "REJECTED", "MISSING"]:
                    return f"STC{ith}"
            return None

        def _cancel_stp_orders():
            for si in range(1, self.max_stc_orders):
                sid = trade.get(f"STC{si}-ordID")
                sstat = trade.get(f"STC{si}-Status")
                if pd.isna(sid) or sid == "":
                    continue
                if sstat in ["FILLED", "EXECUTED", "CANCELED", "REJECTED", "MISSING"]:
                    continue
                _, sinfo = self.get_order_info(sid)
                if sinfo and sinfo.get("orderType") == "STP":
                    self.bksession.cancel_order(sid)
                    self.portfolio.loc[i, f"STC{si}-Status"] = "CANCELED"
                    str_msg = f"Dynamic PT: Cancelled breakeven SL {trade['Symbol']} STC{si}"
                    print(Back.YELLOW + str_msg)
                    self.queue_prints.put([str_msg, "", "yellow"])

        def _submit_order(qty_to_sell, order_type="LMT", sl_price=None):
            nonlocal trade, submitted, new_done
            if qty_to_sell <= 0:
                return
            STC = _find_slot()
            if STC is None:
                return
            order = {
                "Symbol": trade["Symbol"],
                "action": "STC",
                "Qty": qty_to_sell,
            }
            ord_inf = trade["Symbol"].split("_")
            if len(ord_inf) == 2:
                opt_type = "C" if "C" in ord_inf[1] else "P"
                strike = str(re.split("C|P", ord_inf[1])[1]) + opt_type
                order["strike"] = strike
            if order_type == "LMT":
                order["price"] = quote
                order = self.round_order_price(order, trade)
                ord_func = self.bksession.make_STC_lim
                _, STC_ordID = self.bksession.send_order(ord_func(**order))
            elif order_type == "SL":
                order["SL"] = sl_price
                order = self.round_order_price(order, trade)
                sl_price = order["SL"]
                ord_func = self.bksession.make_STC_SL
                _, STC_ordID = self.bksession.send_order(ord_func(**order))
            if STC_ordID is not None:
                self.portfolio.loc[i, f"{STC}-ordID"] = STC_ordID
                self.portfolio.loc[i, f"{STC}-Status"] = "SUBMITTED"
                self.portfolio.loc[i, f"{STC}-Qty"] = qty_to_sell
                self.portfolio.loc[i, f"{STC}-xQty"] = round(qty_to_sell / total_qty, 2)
                trade = self.portfolio.iloc[i]
                submitted = True
                return True
            return False

        new_done = list(done_levels)
        submitted = False

        # 1) Stop-loss check – close all if P&L <= -sl_pct
        if sl_pct > 0 and pnl_pct <= -sl_pct:
            remaining = _calc_remaining()
            if remaining > 0:
                ok = _submit_order(remaining, "SL", entry_price * (1 - sl_pct / 100))
                if ok:
                    new_done = [int(x) for x in levels]
                    str_msg = f"Dynamic PT: SL triggered {trade['Symbol']} P&L={pnl_pct:.1f}% (limit -{sl_pct}%) Qty:{remaining}"
                    print(Back.RED + str_msg)
                    self.queue_prints.put([str_msg, "", "red"])
                    submitted = True

        # 2) Profit-taking levels
        if pnl_pct > 0:
            for level_pct, size_pct in zip(levels, sizes):
                if level_pct in done_levels:
                    continue
                if pnl_pct < level_pct:
                    continue
                remaining = _calc_remaining()
                qty = max(1, round(total_qty * size_pct / 100))
                qty_to_sell = min(qty, remaining)
                if qty_to_sell <= 0:
                    continue
                ok = _submit_order(qty_to_sell, "LMT")
                if ok:
                    str_msg = f"Dynamic PT: {trade['Symbol']} @{quote} (Qty:{qty_to_sell}) at +{level_pct}% P&L"
                    print(Back.GREEN + str_msg)
                    self.queue_prints.put([str_msg, "", "green"])
                    new_done.append(int(level_pct))
                    submitted = True
                    if len(done_levels) > 0:
                        _cancel_stp_orders()

        # 3) Update done levels
        if submitted:
            self.portfolio.loc[i, "dynamic_pt_done"] = (
                ",".join(str(x) for x in new_done) if new_done else ""
            )
            self.save_logs("port")

        # 4) Breakeven stop-loss after first trim (only if SL didn't trigger)
        if (
            do_breakeven
            and not (sl_pct > 0 and pnl_pct <= -sl_pct)
            and len(done_levels) == 0
            and len(new_done) > len(done_levels)
        ):
            remaining = _calc_remaining()
            if remaining > 0:
                _submit_order(remaining, "SL", entry_price)
                str_msg = f"Dynamic PT: Breakeven SL {trade['Symbol']} @{entry_price} (Qty:{remaining})"
                print(Back.GREEN + str_msg)
                self.queue_prints.put([str_msg, "", "green"])

    def close_expired(self, open_trade):
        i = open_trade
        trade = self.portfolio.iloc[i]
        if trade["Asset"] != "option" or trade["isOpen"] == 0:
            return
        optdate = option_date(trade["Symbol"])
        if optdate.date() < date.today():
            expdate = date.today().strftime("%Y-%m-%d %H:%M:%S+0000")
            usold = np.nansum(
                [trade[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
            )
            STC = f"STC{self.max_stc_orders-1}"  # default to max stc
            for stci in range(1, self.max_stc_orders):
                if pd.isnull(trade[f"STC{stci}-Qty"]):
                    STC = f"STC{stci}"
                    break

            price = 0
            pnl = -100
            action = trade["Type"].replace("STO", "BTC").replace("BTO", "STC")
            quote = self.price_now(trade["Symbol"], price_type=action, pflag=1)
            if quote > 0:
                price = quote
                if action == "STC":
                    pnl = (price - trade["Price"]) / trade["Price"] * 100
                else:
                    pnl = (trade["Price"] - price) / trade["Price"] * 100
            # Log portfolio
            self.portfolio.loc[open_trade, STC + "-Status"] = "EXPIRED"
            self.portfolio.loc[open_trade, STC + "-Price"] = price
            self.portfolio.loc[open_trade, STC + "-Date"] = expdate
            self.portfolio.loc[open_trade, STC + "-xQty"] = 1
            self.portfolio.loc[open_trade, STC + "-Qty"] = trade["filledQty"] - usold
            self.portfolio.loc[open_trade, STC + "-PnL"] = pnl
            self.portfolio.loc[open_trade, "isOpen"] = 0

            bto_price = self.portfolio.loc[open_trade, "Price"]
            bto_price_alert = self.portfolio.loc[open_trade, "Price-alert"]
            bto_price_actual = self.portfolio.loc[open_trade, "Price-actual"]

            trade = self.portfolio.loc[open_trade]
            sold_tot = np.nansum(
                [trade[f"STC{i}-Qty"] for i in range(1, self.max_stc_orders)]
            )
            stc_PnL_all = (
                np.nansum(
                    [
                        trade[f"STC{i}-PnL"] * trade[f"STC{i}-Qty"]
                        for i in range(1, self.max_stc_orders)
                    ]
                )
                / sold_tot
            )
            self.portfolio.loc[open_trade, "PnL"] = stc_PnL_all
            if action == "STC":
                stc_PnL_all_alert = (
                    np.nansum(
                        [
                            (
                                float(
                                    (trade[f"STC{i}-Price-alert"] - bto_price_alert)
                                    / bto_price_alert
                                )
                                * 100
                            )
                            * trade[f"STC{i}-Qty"]
                            for i in range(1, self.max_stc_orders)
                        ]
                    )
                    / sold_tot
                )
                stc_PnL_all_curr = (
                    np.nansum(
                        [
                            (
                                float(
                                    (trade[f"STC{i}-Price-actual"] - bto_price_actual)
                                    / bto_price_actual
                                )
                                * 100
                            )
                            * trade[f"STC{i}-Qty"]
                            for i in range(1, self.max_stc_orders)
                        ]
                    )
                    / sold_tot
                )
            else:
                stc_PnL_all_alert = (
                    np.nansum(
                        [
                            (
                                float(
                                    (bto_price_alert - trade[f"STC{i}-Price-alert"])
                                    / bto_price_alert
                                )
                                * 100
                            )
                            * trade[f"STC{i}-Qty"]
                            for i in range(1, self.max_stc_orders)
                        ]
                    )
                    / sold_tot
                )
                stc_PnL_all_curr = (
                    np.nansum(
                        [
                            (
                                float(
                                    (bto_price_alert - trade[f"STC{i}-Price-actual"])
                                    / bto_price_actual
                                )
                                * 100
                            )
                            * trade[f"STC{i}-Qty"]
                            for i in range(1, self.max_stc_orders)
                        ]
                    )
                    / sold_tot
                )

            self.portfolio.loc[open_trade, "PnL-alert"] = stc_PnL_all_alert
            self.portfolio.loc[open_trade, "PnL-actual"] = stc_PnL_all_curr

            mutipl = 1 if trade["Asset"] == "option" else 0.01  # pnl already in %
            self.portfolio.loc[open_trade, "PnL$"] = (
                stc_PnL_all * bto_price * mutipl * sold_tot
            )
            self.portfolio.loc[open_trade, "PnL$-alert"] = (
                stc_PnL_all_alert * bto_price_alert * mutipl * sold_tot
            )
            self.portfolio.loc[open_trade, "PnL$-actual"] = (
                stc_PnL_all_curr * bto_price_actual * mutipl * sold_tot
            )

            str_prt = (
                f"{trade['Symbol']} option expired -100% Qty: {trade['filledQty']}"
            )
            print(Back.GREEN + str_prt)
            self.queue_prints.put([str_prt, "", "green"])
            self.save_logs("port")

    def calculate_stoploss(self, order, trade, SL: str):
        "Calculate stop loss price with increment, SL: e.g. '40%"
        if isinstance(SL, str):
            if "%" in SL:
                SL = trade["Price"] * float(SL.replace("%", "")) / 100
            else:
                SL = float(SL)

        order["trail_stop_const"] = self.round_price(SL, trade)
        return order

    def round_order_price(self, order, trade):
        # Round SL price to nearest increment
        for exit in ["price", "PT", "SL"] + [
            f"PT{i}" for i in range(1, self.max_stc_orders)
        ]:
            if order.get(exit) is not None and isinstance(
                order.get(exit), (int, float)
            ):
                order[exit] = self.round_price(order.get(exit), trade)
        return order

    def round_price(self, price, trade):
        # Round SL price to nearest increment
        if self.bksession.name in ["tda"]:
            if "SPXW" in trade["Symbol"]:
                if price < 3.0:
                    increment = 0.05
                else:
                    increment = 0.10
            else:
                increment = 0.01
        elif self.bksession.name == "ts":
            if "SPXW" in trade["Symbol"]:
                if price < 3.0:
                    increment = 0.05
                else:
                    increment = 0.10
            elif price > 3:
                increment = 0.05
            else:
                increment = 0.01
        elif (
            trade["Symbol"] in ["SPY", "QQQ", "IWM"] and self.bksession.name == "etrade"
        ):
            increment = 0.01  # ETFs trade in penny increments
        elif self.bksession.name == "etrade":
            if price < 3.0:
                increment = 0.05
            else:
                increment = 0.10
        else:
            increment = 0.01

        new_price = round(round(price / increment) * increment, 2)
        if new_price == 0:
            new_price = increment
        return new_price


def option_date(opt_symbol):
    sym_inf = opt_symbol.split("_")[1]
    opt_date = re.split("C|P", sym_inf)[0]
    return datetime.strptime(opt_date, "%m%d%y")


def amnt_left(order, position):
    # Calculate amnt to sell based on alerted left amount
    available = position["Qty"]
    if order.get("amnt_left"):
        left = order["amnt_left"]
        if left == "few":
            order["xQty"] = 1 - 0.2
            order["Qty"] = max(round(available * order["xQty"]), 1)
        elif left > 0.99:  # unit left
            order["Qty"] = max(available - left, 1)
            order["xQty"] = (available - order["Qty"]) / available
        elif left < 0.99:  # percentage left
            order["xQty"] = 1 - left
            order["Qty"] = max(round(available * order["xQty"]), 1)
        else:
            raise ValueError
        return order, True
    else:
        return order, False


if __name__ == "__main__":
    from DiscordAlertsTrader.brokerages import get_brokerage

    bksession = get_brokerage()
    at = AlertsTrader(bksession)

    order = {
        "action": "STO",
        "Symbol": "JL",
        "Qty": 1,
        "price": 0.78,
        "Trader": "me",
        "asset": "stock",
        "risk": 1,
        "price_actual": 0.80,
    }
    at.new_trade_alert(order, "pars", "msg")
