
import os
import time
import pandas as pd
from datetime import datetime, timezone, date
import threading
from colorama import Fore, init
import discord # this is discord.py-self package not discord

from DiscordAlertsTrader.message_parser import parse_trade_alert
from DiscordAlertsTrader.configurator import cfg
from DiscordAlertsTrader.configurator import channel_ids
from DiscordAlertsTrader.alerts_trader import AlertsTrader
from DiscordAlertsTrader.alerts_tracker import AlertsTracker
from DiscordAlertsTrader.server_alert_formatting import server_formatting
try:
    from .custom_msg_format import msg_custom_formated, msg_custom_formated2
    print("custom message format loaded")
    custom = True
except ImportError:
    custom = False


init(autoreset=True)

class dummy_queue():
    def __init__(self, maxsize=10):
        self.maxsize = maxsize
        self.queue = []

    def put(self, item):
        if len(self.queue) >= self.maxsize:
            self.queue.pop(0)
        self.queue.append(item)

def split_strip(string):
    lstr = string.split(",")
    lstr = [s.strip().lower() for s in lstr]
    return lstr

class DiscordBot(discord.Client):
    def __init__(self, 
                 queue_prints=dummy_queue(maxsize=10), 
                 live_quotes=True, 
                 brokerage=None,
                 tracker_portfolio_fname=cfg['portfolio_names']["tracker_portfolio_name"],
                 cfg = cfg):
        super().__init__()
        self.channel_IDS = channel_ids
        self.time_strf = "%Y-%m-%d %H:%M:%S.%f"
        self.queue_prints = queue_prints
        self.bksession = brokerage
        self.live_quotes = live_quotes
        self.cfg = cfg
        if brokerage is not None:
            self.trader = AlertsTrader(queue_prints=self.queue_prints, brokerage=brokerage, cfg=self.cfg)
        else:
            self.trader = None
        self.tracker = AlertsTracker(brokerage=brokerage, portfolio_fname=tracker_portfolio_fname, cfg=self.cfg)
        self.load_data()        

        if (live_quotes and brokerage is not None and brokerage.name != 'webull') \
            or (brokerage is not None and brokerage.name == 'webull' and 
                cfg['general'].getboolean('webull_live_quotes')):
            self.thread_liveq =  threading.Thread(target=self.track_live_quotes)
            self.thread_liveq.start()

    def close_bot(self):
        if self.bksession is not None:
            self.trader.update_portfolio = False
            self.live_quotes = False

    def connect_broker(self, brokerage):
        """Connect broker after initial Discord-only startup"""
        if self.trader is not None:
            self.trader.update_portfolio = False
        self.bksession = brokerage
        self.trader = AlertsTrader(queue_prints=self.queue_prints, brokerage=brokerage, cfg=self.cfg)
        self.tracker = AlertsTracker(brokerage=brokerage,
                                     portfolio_fname=self.cfg['portfolio_names']['tracker_portfolio_name'],
                                     cfg=self.cfg)
        self.load_data()
        self.trader.sync_ibkr_positions()
        if self.live_quotes:
            self.thread_liveq = threading.Thread(target=self.track_live_quotes, daemon=True)
            self.thread_liveq.start()

    def track_live_quotes(self):
        dir_quotes = self.cfg['general']['data_dir'] + '/live_quotes'
        os.makedirs(dir_quotes, exist_ok=True)

        while self.live_quotes:
            # Skip closed market
            now = datetime.now()
            weekday, hour = now.weekday(), now.hour
            after_hr, before_hr = self.cfg['general']['off_hours'].split(",")
            if  weekday >= 5 or (hour < int(before_hr) or hour >= int(after_hr)):  
                time.sleep(60)
                continue

            # get unique symbols  from portfolios, either options or all, open or alerted today
            tk_day = pd.to_datetime(self.tracker.portfolio['Date']).dt.date == date.today()
            td_day = pd.to_datetime(self.trader.portfolio['Date']).dt.date == date.today()
            msk_tk = ((self.tracker.portfolio['isOpen']==1) | tk_day) 
            msk_td = ((self.trader.portfolio['isOpen']==1) | td_day) 
            
            if self.cfg['general'].getboolean('live_quotes_options_only'):
                msk_tk = msk_tk & (self.tracker.portfolio['Asset']=='option')
                msk_td = msk_td & (self.trader.portfolio['Asset']=='option')
            
            track_symb = set(self.tracker.portfolio.loc[msk_tk, 'Symbol'].to_list() + \
                self.trader.portfolio.loc[msk_td, 'Symbol'].to_list())
            if not len(track_symb):
                time.sleep(5)
                continue
            # save quotes to file
            try:
                quote = self.bksession.get_quotes(track_symb)
            except Exception as e:
                print('error during live quote:', e)
                continue
            if quote is None:
                continue
            
            for q in quote: 
                if quote[q].get('description') == 'Symbol not found' or q =='' or quote[q]['bidPrice'] == 0:
                    continue
                timestamp = quote[q]['quoteTimeInLong']//1000  # in ms

                # Read the last line of the file and get the last recorded timestamp for the symbol
                file_path = f"{dir_quotes}/{quote[q]['symbol']}.csv"
                last_line = ""
                do_header = True
                if os.path.exists(file_path):
                    do_header = False
                    with open(file_path, "r") as f:
                        lines = f.readlines()
                        if lines:
                            last_line = lines[-1].strip()
                
                #if last recorded timestamp is the same as current, skip
                if len(last_line) and float(last_line.split(",")[1] )== quote[q]['bidPrice'] and float(last_line.split(",")[2] )== quote[q]['askPrice']:
                    continue
                
                # Write the new line to the file
                with open(file_path, "a+") as f:
                    if do_header:
                        f.write(f"timestamp, quote, quote_ask\n")
                    f.write(f"{timestamp}, {quote[q]['bidPrice']}, {quote[q]['askPrice']}\n")
            
            # Sleep for up to X secs    
            toc = (datetime.now() - now).total_seconds()
            if toc < float(cfg['general']['sampling_rate_quotes']) and self.live_quotes:
                time.sleep(float(cfg['general']['sampling_rate_quotes'])-toc)

    def load_data(self):
        self.chn_hist= {}
        self.chn_hist_fname = {}
        for ch in self.channel_IDS.keys():
            dt_fname = f"{self.cfg['general']['data_dir']}/{ch}_message_history.csv"
            if not os.path.exists(dt_fname):
                ch_dt = pd.DataFrame(columns=self.cfg['col_names']['chan_hist'].split(","))
                ch_dt.to_csv(dt_fname, index=False)
                ch_dt.to_csv(f"{self.cfg['general']['data_dir']}/{ch}_message_history_temp.csv", index=False)
            else:
                ch_dt = pd.read_csv(dt_fname)
                col_map = {c: c.title() for c in ch_dt.columns}
                col_map['authorid'] = 'AuthorID'
                col_map['author_id'] = 'AuthorID'
                col_map['author'] = 'Author'
                col_map['date'] = 'Date'
                col_map['content'] = 'Content'
                col_map['parsed'] = 'Parsed'
                col_map['channel'] = 'Channel'
                ch_dt.rename(columns=col_map, inplace=True)
                ch_dt = ch_dt.loc[:, ~ch_dt.columns.duplicated()]

            self.chn_hist_fname[ch] = dt_fname
            self.chn_hist[ch]= ch_dt

    async def on_ready(self):
        print('Logged on as', self.user , '\n loading previous messages')        
        await self.load_previous_msgs()
    
    async def on_message(self, message):
        # only respond to channels in config or authorwise subscription
        author = f"{message.author.name}#{message.author.discriminator}".replace("#0", "")
        
        if message.channel.id == int(cfg['discord']['commands_channel']):
            if message.content.startswith('!close long'):
                cfg['general']['DO_BTO_TRADES'] = 'false'
                print("BTC trades closed")
            elif message.content.startswith('!close short'):
                cfg['shorting']['DO_STO_TRADES'] = 'false'
                print("STO trades closed")
            elif message.content.startswith('!open long'):
                cfg['general']['DO_BTO_TRADES'] = 'true'
                print("BTC trades opened")
            elif message.content.startswith('!open short'):
                cfg['shorting']['DO_STO_TRADES'] = 'true'
                print("STO trades opened")
            return
        elif message.channel.id not in self.channel_IDS.values() and \
            author.lower() not in split_strip(self.cfg['discord']['authorwise_subscription']):
            return
        if message.content == 'ping':
            await message.channel.send('pong')
         
        
        message = server_formatting(message)
        if custom:
            await msg_custom_formated2(message)
            alert = msg_custom_formated(message, self.bksession)
            if alert is not None:
                for msg in alert:
                    self.new_msg_acts(msg, False)
                return
        
        if not len(message.content):
            return
        self.new_msg_acts(message)

    # async def on_message_edit(self, before, after):
    #     # Ignore if the message is not from a user or if the bot itself edited the message
    #     if after.channel.id not in self.channel_IDS.values() or  before.author.bot:
    #         return

    #     str_prt = f"Message edited by {before.author}: '{before.content}' -> '{after.content}'"
    #     self.queue_prints.put([str_prt, "black"])
    #     print(Fore.BLUE + str_prt)

    async def load_previous_msgs(self):
        await self.wait_until_ready()
        for ch, ch_id in self.channel_IDS.items():
            channel = self.get_channel(ch_id)
            if channel is None:
                print("channel not found:", ch)
                continue
            
            if len(self.chn_hist[ch]):
                msg_last = self.chn_hist[ch].iloc[-1]
                last_date = msg_last.get('Date', '')
                if pd.isna(last_date) or not last_date:
                    continue
                date_After = datetime.strptime(last_date, self.time_strf)
                iterator = channel.history(after=date_After, oldest_first=True)
            else:
                # iterator = channel.history(oldest_first=True)
                continue
                
            print("In", channel)
            async for message in iterator:
                message = server_formatting(message)
                if message is None:
                    continue
                if custom:
                    alert = msg_custom_formated(message)
                    if alert is not None:
                        for msg in alert:
                            self.new_msg_acts(msg, False)
                else:
                    self.new_msg_acts(message)
                # if custom:
                #     await msg_custom_formated2(message)
        print("Done")        
        self.tracker.close_expired()

    def new_msg_acts(self, message, from_disc=True):
        if from_disc:
            msg_date = message.created_at.replace(tzinfo=timezone.utc).astimezone(tz=None)
            msg_date_f = msg_date.strftime(self.time_strf)    
            if message.channel.id in self.channel_IDS.values():
                chn_ix = list(self.channel_IDS.values()).index(message.channel.id)
                chn = list(self.channel_IDS.keys())[chn_ix]
            else:
                chn = None
            msg = pd.Series({'AuthorID': message.author.id,
                            'Author': f"{message.author.name}#{message.author.discriminator}".replace("#0", ""),
                            'Date': msg_date_f, 
                            'Content': message.content,
                            'Channel': chn
                            })
        else:
            msg = message
        chn = msg['Channel']
        shrt_date = datetime.strptime(msg["Date"], self.time_strf).strftime('%Y-%m-%d %H:%M:%S')
        self.queue_prints.put([f"\n{shrt_date} {msg['Channel']}: \n\t{msg['Author']}: {msg['Content']} ", "blue"])
        print(Fore.BLUE + f"{shrt_date} \t {msg['Author']}: {msg['Content']} ")

        pars, order =  parse_trade_alert(msg['Content'])
        if pars is None:
            pars, order = parse_trade_alert("BTO " + msg['Content'])
            if pars is not None:
                print("[TRACE 1b] Parsed after adding BTO prefix")
        
        if pars is None:
            print("[TRACE 1] parse_trade_alert returned None → not a trade alert, skip")
            if self.chn_hist.get(chn) is not None:
                msg['Parsed'] = ""
                self.chn_hist[chn] = pd.concat([self.chn_hist[chn], msg.to_frame().transpose()],axis=0, ignore_index=True)
                self.chn_hist[chn].to_csv(self.chn_hist_fname[chn], index=False)
            return
        
        print(f"[TRACE 2] Parsed OK → action={order.get('action')} symbol={order.get('Symbol')} price={order.get('price')}")
        if order['asset'] == "option":
            try:
                if len(order['expDate'].split("/")) ==2:
                    exp_dt = datetime.strptime(f"{order['expDate']}/{datetime.now().year}" , "%m/%d/%Y").date()
                else:
                    if len(order['expDate'].split("/")[-1]) == 2:
                        exp_dt = datetime.strptime(f"{order['expDate']}" , "%m/%d/%y").date()
                    else:
                        exp_dt = datetime.strptime(f"{order['expDate']}", "%m/%d/%Y").date()
            except ValueError:
                str_msg = f"Option date is wrong: {order['expDate']}"
                print(f"[TRACE 3] Option date error: {order['expDate']}")
                self.queue_prints.put([f"\t {str_msg}", "green"])
                print(Fore.GREEN + f"\t {str_msg}")
                msg['Parsed'] = str_msg
                if self.chn_hist.get(chn) is not None:
                    self.chn_hist[chn] = pd.concat([self.chn_hist[chn], msg.to_frame().transpose()],axis=0, ignore_index=True)
                    self.chn_hist[chn].to_csv(self.chn_hist_fname[chn], index=False)
                return

            dt = datetime.now().date()
            order['dte'] =  (exp_dt - dt).days
            print(f"[TRACE 3b] Option DTE = {order['dte']}")
            if order['dte']<0:
                str_msg = f"Option date in the past: {order['expDate']}"
                print(f"[TRACE 4] Option expired (DTE={order['dte']}), skip")
                self.queue_prints.put([f"\t {str_msg}", "green"])
                print(Fore.GREEN + f"\t {str_msg}")
                msg['Parsed'] = str_msg
                if self.chn_hist.get(chn) is not None:
                    self.chn_hist[chn] = pd.concat([self.chn_hist[chn], msg.to_frame().transpose()],axis=0, ignore_index=True)
                    self.chn_hist[chn].to_csv(self.chn_hist_fname[chn], index=False)
                return

        order['Trader'], order["Date"] = msg['Author'], msg["Date"]
        order_date = datetime.strptime(order["Date"], "%Y-%m-%d %H:%M:%S.%f")
        date_diff = abs(datetime.now() - order_date)
        print(f"time difference is {date_diff.total_seconds()}")

        live_alert = True if date_diff.seconds < 90 else False
        print(f"[TRACE 5] live_alert={live_alert} (date_diff.seconds={date_diff.seconds} < 90)")
        str_msg = pars
        if live_alert:
            if self.bksession is None:
                print(f"[TRACE 5a] bksession is None → skip trade")
        self.queue_prints.put([f"\t {str_msg}", "green"])
        print(Fore.GREEN + f"\t {str_msg}")
        #Tracker
        if chn != "GUI_user":
            track_out = self.tracker.trade_alert(order, live_alert, chn)
            self.queue_prints.put([f"{track_out}", "red"])
        # Trader
        do_trade, order = self.do_trade_alert(msg['Author'], msg['Channel'], order)
        print(f"[TRACE 9] do_trade_alert returned: do_trade={do_trade}, seconds={date_diff.seconds}")
        if do_trade and date_diff.seconds < 120:
            print(f"[TRACE 10] Calling trader.new_trade_alert()...")
            order["Trader"] = msg['Author']
            self.trader.new_trade_alert(order, pars, msg['Content'])
        else:
            if not do_trade:
                print(f"[TRACE 10a] Skipped: do_trade=False")
            if date_diff.seconds >= 120:
                print(f"[TRACE 10b] Skipped: seconds={date_diff.seconds} >= 120")

        if self.chn_hist.get(chn) is not None:
            msg['Parsed'] = pars
            self.chn_hist[chn] = pd.concat([self.chn_hist[chn], msg.to_frame().transpose()],axis=0, ignore_index=True)
            self.chn_hist[chn].to_csv(self.chn_hist_fname[chn], index=False)
    
    def do_trade_alert(self, author, channel, order):
        "Decide if alert should be traded"
        if self.bksession is None:
            print(f"[TRACE do_trade] bksession is None → no trade")
            return False, order
        if channel == "GUI_analysts":
            print(f"[TRACE do_trade] channel=GUI_analysts → no trade")
            return False, order

        # in authors subs list or channel subs list
        auth_ok = author.lower() in split_strip(self.cfg['discord']['authors_subscribed'])
        chan_ok = channel.lower() in split_strip(self.cfg['discord']['channelwise_subscription'])
        print(f"[TRACE do_trade] author='{author}' in subscribed={auth_ok}, channel='{channel}' in cw_sub={chan_ok}")
        if auth_ok or chan_ok:
            # ignore if no STC
            if not self.cfg['general'].getboolean('DO_STC_TRADES') and order['action'] == "STC" \
            and channel not in ["GUI_user", "GUI_both"]:        
                str_msg = f"STC not accepted by config options: DO_STC_TRADES = False"
                print(f"[TRACE do_trade] DO_STC_TRADES=False, reject STC")
                print(Fore.GREEN + str_msg)
                self.queue_prints.put([str_msg, "", "green"])
                return False, order
            else:
                if order['action'] == "BTO" and order['asset'] == 'option':
                    min_price = cfg['order_configs']['min_opt_price']
                    if len(min_price) and order['price'] *100 < float(cfg['order_configs']['min_opt_price']):
                        str_msg = f"Option price is too small as per config: {order['price']}"
                        print(f"[TRACE do_trade] price={order['price']} below min_opt_price={min_price}, reject")
                        print(Fore.GREEN + str_msg)
                        self.queue_prints.put([str_msg, "", "green"])
                        return False, order
                print(f"[TRACE do_trade] All checks passed → TRADE YES")
                return True, order

        # in authors shorting list
        elif author.lower() in split_strip(self.cfg['shorting']['authors_subscribed']):
            print(f"[TRACE do_trade] author in shorting list")

            # short order sent manullay from gui
            if order["action"] in ["BTC", "STO"] and channel in ["GUI_user", "GUI_both"]:
                print(f"[TRACE do_trade] Short GUI order → TRADE YES")
                return True, order
            # Make it shorting order
            order["action"] = "STO" if order["action"] == "BTO" else "BTC" if order["action"] == "STC" else order["action"]
            # reject if cfg do BTO or STC is false
            if (order["action"] == "BTC" and not self.cfg['shorting'].getboolean('DO_BTC_TRADES')) \
                or (order["action"] == "STO" and not self.cfg['shorting'].getboolean('DO_STO_TRADES')):
                print(f"[TRACE do_trade] Short action blocked by config")
                return False, order
            
            if order['asset'] == 'stock':
                print(f"[TRACE do_trade] Short stock → TRADE YES")
                return True, order
            
            if len(self.cfg['shorting']['max_dte']):
                if order['dte'] <= int(self.cfg['shorting']['max_dte']):
                    print(f"[TRACE do_trade] Short option DTE OK → TRADE YES")
                    return True, order
                else:
                    str_msg = f"STO {order['dte']} DTE smaller than max in config: {self.cfg['shorting']['max_dte']}, order aborted"
                    print(f"[TRACE do_trade] DTE {order['dte']} > max {self.cfg['shorting']['max_dte']}, reject")
                    print(Fore.RED + str_msg)
                    self.queue_prints.put([str_msg, "", "red"])
                    return False, order
        else:
            print(f"[TRACE do_trade] author not in subscribed or shorting lists → no trade")
                
        return False, order

if __name__ == '__main__':
    from DiscordAlertsTrader.configurator import cfg, channel_ids
    from DiscordAlertsTrader.brokerages import get_brokerage
    bksession = get_brokerage()
    client = DiscordBot(brokerage=bksession, cfg=cfg, live_quotes=False)
    client.run(cfg['discord']['discord_token'])



