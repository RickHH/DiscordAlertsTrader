from DiscordAlertsTrader.brokerages import BaseBroker
from DiscordAlertsTrader.configurator import cfg
from datetime import datetime
import time
import asyncio
import random
import threading
import queue

# ib_async imported lazily inside _worker() where the event loop exists

class IBKR(BaseBroker):
    def __init__(self, accountId=None):
        self.name = 'ibkr'
        self.accountId = accountId
        self._conId_cache = {}
        self._oca_pairs = {}
        self._lock = threading.RLock()

        self._wk_error = None
        self._wk_queue = queue.Queue()
        self._wk_results = {}
        self._wk_lock = threading.Lock()
        self._wk_seq = 0
        self._wk_ready = threading.Event()

        t = threading.Thread(target=self._worker, daemon=True, name='ibkr-worker')
        t.start()
        self._wk_ready.wait()
        if self._wk_error:
            raise self._wk_error

    def _worker(self):
        self._wk_error = None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # ib_async must be imported inside worker thread where event loop exists
        import nest_asyncio
        import ib_async
        self._ib = ib_async
        nest_asyncio.apply(loop)
        try:
            self.ib = self._ib.IB()
            self.ib.connect(cfg['IBKR']['host'], cfg['IBKR']['port'],
                            clientId=random.randint(1, 1000))
            self.ib.sleep(0.2)
        except Exception as e:
            self._wk_error = e
        finally:
            self._wk_ready.set()

        while True:
            work_id, fn, ev = self._wk_queue.get()
            try:
                result = fn()
            except Exception as e:
                result = e
            with self._wk_lock:
                self._wk_results[work_id] = result
            ev.set()

    def _call(self, fn, timeout=90):
        ev = threading.Event()
        with self._wk_lock:
            work_id = self._wk_seq
            self._wk_seq += 1
        self._wk_queue.put((work_id, fn, ev))
        if not ev.wait(timeout=timeout):
            raise TimeoutError(f"IBKR call timed out")
        with self._wk_lock:
            result = self._wk_results.pop(work_id)
        if isinstance(result, Exception):
            raise result
        return result

    def get_session(self):
        return self._call(lambda: self.ib.isConnected())

    def get_account_info(self):
        return self._call(self._get_account_info_impl)

    def _get_account_info_impl(self):
        data = self.ib.accountValues()
        for item in data:
            if item.tag == 'NetLiquidation':
                liquidationValue = item.value
            if item.tag == 'CashBalance':
                cashBalance = item.value
            if item.tag == 'AvailableFunds':
                availableFunds = item.value

        acc_inf = {
            'securitiesAccount':{
                'positions':[],
                'accountId' : str(self.accountId),
                'currentBalances':{
                    'liquidationValue': liquidationValue,
                    'cashBalance': cashBalance,
                    'availableFunds': availableFunds,
                },
        }}

        self.ib.sleep(0.1)
        positions = self.ib.portfolio()
        self.ib.sleep(0.1)

        for position in positions:
            marketValue = position.marketValue
            unrealizedPnL = position.unrealizedPNL
            unrealizedPnLPercentage = unrealizedPnL / marketValue if marketValue != 0 else 0

            contract = position.contract
            if contract.secType == 'OPT':
                full_sym = self._convert_option_from_ibkr(contract)
            else:
                full_sym = contract.symbol

            pos_d = {
                "longQuantity" : position.position,
                "symbol": contract.symbol,
                "fullSymbol": full_sym,
                "marketValue": marketValue,
                "assetType": contract.secType,
                "averagePrice": position.averageCost,
                "currentDayProfitLoss": unrealizedPnL,
                "currentDayProfitLossPercentage": unrealizedPnLPercentage,
                'instrument': {'symbol': contract.symbol,
                                'assetType': contract.secType,
                                }
                }
            acc_inf['securitiesAccount']['positions'].append(pos_d)

        if not len(positions):
            acc_inf['securitiesAccount']['positions'] = []

        self.ib.sleep(0.1)
        trades = self.ib.trades()
        orders_inf =[]

        for trade in trades:
            formatted_order = self.format_order(trade)
            orders_inf.append(formatted_order)

        acc_inf['securitiesAccount']['orderStrategies'] = orders_inf
        return acc_inf

    def format_order(self, trade):
        order = trade.order
        status = trade.orderStatus.status
        status = status.upper().replace('SUBMITTED','WORKING').replace('CANCELLED','CANCELED').replace("PENDINGSUBMIT", "WORKING")

        placedTime = trade.log[0].time.timestamp() if trade.log else None
        placedTime = datetime.fromtimestamp(placedTime).strftime("%Y-%m-%dT%H:%M:%S+00") if placedTime else None

        enteredTime = trade.fills[0].time if trade.fills else None
        if isinstance(enteredTime, float ) and enteredTime is not None:
            enteredTime = datetime.fromtimestamp(enteredTime)
        enteredTime = enteredTime.strftime("%Y-%m-%dT%H:%M:%S+00") if enteredTime else None

        closeTime  = trade.fills[-1].time if trade.fills else None
        if isinstance(closeTime, float ) and closeTime is not None:
            closeTime = datetime.fromtimestamp(closeTime)
        closeTime  = closeTime.strftime("%Y-%m-%dT%H:%M:%S+00") if closeTime else None

        qty = order.totalQuantity
        if trade.orderStatus.status.upper() == 'FILLED' and order.totalQuantity == 0 and order.filledQuantity > 0:
            qty = order.filledQuantity

        price = trade.orderStatus.avgFillPrice
        if price == 0 and trade.fills:
            price = trade.fills[0].execution.price
        elif price == 0 and trade.order.lmtPrice:
            price = trade.order.lmtPrice

        order_info = {
            'status': status,
            'quantity': qty,
            'filledQuantity': order.filledQuantity if order.filledQuantity>0 and order.filledQuantity <= order.totalQuantity else 0,
            'price': price,
            'orderStrategyType': 'SINGLE',
            "order_id" : order.orderId,
            "orderId": order.orderId,
            "stopPrice": order.auxPrice if order.orderType == 'STP' else None,
            'orderType':  order.orderType,
            'placedTime': placedTime,
            'enteredTime': enteredTime,
            "closeTime": closeTime,
            'orderLegCollection':[{
                'instrument':{'symbol':trade.contract.symbol},
                'instruction': order.action,
                'quantity': order.filledQuantity,
            }]
        }
        return order_info

    def cancel_order(self, order_id):
        return self._call(lambda: self._cancel_order_impl(order_id))

    def _cancel_order_impl(self, order_id):
        open_orders = self.ib.openOrders()
        for order in open_orders:
            if order.orderId == order_id:
                self.ib.cancelOrder(order)
                self.ib.sleep(0.1)
                return "Canceled"
        return False

    def get_orders(self):
        return self._call(self._get_orders_impl)

    def _get_orders_impl(self):
        trades = self.ib.trades()
        orders_inf =[]
        for trade in trades:
            formatted_order = self.format_order(trade)
            orders_inf.append(formatted_order)
        return orders_inf

    def get_order_info(self, order_id):
        return self._call(lambda: self._get_order_info_impl(order_id))

    def _get_order_info_impl(self, order_id):
        if not self.ib.isConnected():
            return 'MISSING', None

        oid = int(order_id)
        target_ids = {oid}
        if oid in self._oca_pairs:
            target_ids.add(self._oca_pairs[oid])
        for k, v in list(self._oca_pairs.items()):
            if v == oid:
                target_ids.add(k)

        for trade in self.ib.trades():
            if trade.order.orderId in target_ids:
                formatted = self.format_order(trade)
                status = formatted['status']
                if status in ['FILLED', 'EXECUTED']:
                    return status, formatted
                return status, formatted

        for o in self.ib.openOrders():
            if o.orderId in target_ids:
                return 'WORKING', None

        self.ib.sleep(0.5)
        for trade in self.ib.trades():
            if trade.order.orderId in target_ids:
                formatted = self.format_order(trade)
                return formatted['status'], formatted

        return 'MISSING', None

    def fix_symbol(self, symbol, direction):
        if direction == 'in':
            return symbol.replace("SPXW", "SPX").replace("NDXP", "NDX")
        elif direction == 'out':
            return symbol.replace("SPX", "SPXW").replace("NDX", "NDXP")

    def send_order(self, order_dict):
        return self._call(lambda: self._send_order_impl(order_dict))

    def _send_order_impl(self, order_dict):
        if order_dict.get('conId'):
            contract = self._ib.Contract(conId=order_dict['conId'], exchange='SMART', currency='USD')
        elif order_dict.get('asset') == 'OPT' and '_' in order_dict.get('symbol_str',''):
            contract = self._convert_option_to_ibkr(order_dict['symbol_str'])
        elif order_dict.get('stock'):
            contract = self._ib.Contract(symbol=order_dict['stock'], exchange='SMART', currency='USD',
                                secType='STK' if order_dict.get('asset')!='OPT' else 'OPT')
        else:
            print("send_order: no conId or symbol info available")
            return None, None

        if order_dict.get('orderType') == 'OCA':
            order1 = self._ib.Order()
            order1.action = order_dict['action']
            order1.totalQuantity = order_dict['quant']
            order1.orderType = 'LMT'
            order1.lmtPrice = order_dict['takeProfit']
            order1.transmit = False

            order2 = self._ib.Order()
            order2.action = order_dict['action']
            order2.totalQuantity = order_dict['quant']
            order2.orderType = 'STP'
            order2.auxPrice = order_dict['stopLoss']
            order2.transmit = True

            oca_orders = [order1, order2]
            _trades = self.ib.oneCancelsAll(oca_orders, ocaGroup="OCA_" + str(time.time()), ocaType=1)
            self.ib.sleep(0.3)

            for t in _trades:
                self.ib.placeOrder(contract, t.order)
            self.ib.sleep(0.5)

            if _trades and len(_trades) >= 2:
                id1 = _trades[0].order.orderId
                id2 = _trades[1].order.orderId
                self._oca_pairs[id1] = id2
                return "WORKING", id1
            return "WORKING", None

        order = self._ib.Order()
        order.action = order_dict['action']
        order.totalQuantity = order_dict['quant']
        order.orderType = order_dict['orderType']
        order.tif = order_dict['enforce']
        order.transmit = True
        order.outsideRth = order_dict.get('outsideRegularTradingHour', False)

        if order.orderType == 'LMT':
            order.lmtPrice = order_dict['lmtPrice']
        elif order.orderType == 'STP':
            order.auxPrice = order_dict['stpPrice']
        elif order.orderType == 'TRAIL':
            order.trailStopPrice = order_dict['trial_value']
        elif order.orderType == 'STP LMT':
            order.lmtPrice = order_dict['lmtPrice']
            order.auxPrice = order_dict['stpPrice']

        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1.0)

        status_raw = trade.orderStatus.status
        status = status_raw.upper().replace('SUBMITTED', 'WORKING').replace('PENDINGSUBMIT', 'WORKING')

        return status, order.orderId

    def get_con_id(self, symbol:str):
        with self._lock:
            if symbol in self._conId_cache:
                return self._conId_cache[symbol]
        return self._call(lambda: self._get_con_id_impl(symbol))

    def _get_con_id_impl(self, symbol):
        try:
            if "_" in symbol:
                contract = self._convert_option_to_ibkr(symbol)
                details = self.ib.reqContractDetails(contract)
            else:
                details = self.ib.reqContractDetails(self._ib.Contract(symbol=symbol, secType='STK', exchange='SMART', currency='USD'))
            if details:
                conId = details[0].contract.conId
                with self._lock:
                    self._conId_cache[symbol] = conId
                return conId
        except Exception as e:
            print(f"Error qualifying {symbol}: {e}")
        return None

    def make_BTO_lim_order(self, Symbol:str, Qty:int, price:float, action="BTO", **kwarg):
        kwargs = {}
        if action == "BTO":
            kwargs['action'] = "BUY"
        elif action == "STO":
            kwargs['action'] = "SELL"

        if (("_" in Symbol) or (" " in Symbol)):
            kwargs['asset'] = 'OPT'
            kwargs['outsideRegularTradingHour'] = True
        else:
            kwargs['asset'] ='STK'
            kwargs['outsideRegularTradingHour'] = True
            kwargs['stock'] = Symbol

        kwargs['enforce'] ='GTC'
        kwargs['quant'] = Qty
        kwargs['orderType'] = 'LMT'
        kwargs['lmtPrice'] = price
        if Symbol.startswith("SPXW"):
            kwargs['lmtPrice'] = round(price / 0.05) * 0.05
        kwargs['conId'] = self.get_con_id(Symbol)
        kwargs['symbol_str'] = Symbol
        return kwargs

    def make_Lim_SL_order(self, Symbol:str, Qty:int,  PT:float, SL:float, action="STC",  **kwarg):
        kwargs = {}
        if action == "STC":
            kwargs['action'] = "SELL"
        elif action == "BTC":
            kwargs['action'] = "BUY"

        if (("_" in Symbol) or (" " in Symbol)):
            kwargs['asset'] = 'OPT'
        else:
            kwargs['asset'] ='STK'
            kwargs['outsideRegularTradingHour'] = True
            kwargs['stock'] = Symbol

        kwargs['enforce'] ='GTC'
        kwargs['quant'] = Qty
        kwargs['conId'] = self.get_con_id(Symbol)
        kwargs['takeProfit'] = PT
        kwargs['stopLoss'] = SL
        kwargs['orderType'] = 'OCA'
        return kwargs if kwargs['conId'] is not None else None

    def make_STC_lim(self, Symbol:str, Qty:int, price:float, strike=None, action="STC", **kwarg):
        kwargs = {}
        if action == "STC":
            kwargs['action'] = "SELL"
        elif action == "BTC":
            kwargs['action'] = "BUY"

        if "_" in Symbol:
            kwargs['asset'] = 'OPT'
        else:
            kwargs['asset'] ='STK'
            kwargs['outsideRegularTradingHour'] = True
            kwargs['stock'] = Symbol

        kwargs['enforce'] ='GTC'
        kwargs['quant'] = Qty
        kwargs['orderType'] = 'LMT'
        kwargs['lmtPrice'] = price
        kwargs['conId'] = self.get_con_id(Symbol)
        return kwargs if kwargs['conId'] is not None else None

    def make_STC_SL(self, Symbol:str, Qty:int, SL:float, action="STC", **kwarg):
        kwargs = {}
        if action == "STC":
            kwargs['action'] = "SELL"
        elif action == "BTC":
            kwargs['action'] = "BUY"

        if "_" in Symbol:
            kwargs['asset'] = 'OPT'
        else:
            kwargs['asset'] ='STK'
            kwargs['outsideRegularTradingHour'] = False
            kwargs['stock'] = Symbol

        kwargs['enforce'] ='GTC'
        kwargs['quant'] = Qty
        kwargs['orderType'] = 'STP'
        kwargs['stpPrice'] = SL
        kwargs['lmtPrice'] = SL
        kwargs['conId'] = self.get_con_id(Symbol)
        return kwargs if kwargs['conId'] is not None else None

    def make_STC_SL_trailstop(self, Symbol:str, Qty:int,  trail_stop_const:float, action="STC", **kwarg):
        kwargs = {}
        if action == "STC":
            kwargs['action'] = "SELL"
        elif action == "BTC":
            kwargs['action'] = "BUY"

        if "_" in Symbol:
            kwargs['asset'] = 'OPT'
        else:
            kwargs['asset'] ='STK'
            kwargs['outsideRegularTradingHour'] = True
            kwargs['stock'] = Symbol

        kwargs['enforce'] ='GTC'
        kwargs['quant'] = Qty
        kwargs['orderType'] = 'TRAIL'
        kwargs['trial_value'] = trail_stop_const
        kwargs['trial_type'] = 'DOLLAR'
        kwargs['outsideRegularTradingHour'] = True
        kwargs['conId'] = self.get_con_id(Symbol)
        return kwargs if kwargs['conId'] is not None else None

    def get_quotes(self, symbols:list):
        return self._call(lambda: self._get_quotes_impl(symbols))

    def _get_quotes_impl(self, symbols):
        quotes = {}
        for symbol in symbols:
            try:
                con_id = self._get_con_id_impl(symbol)
                if con_id is None:
                    continue
                contract = self._ib.Contract(conId=con_id, exchange='SMART', currency='USD')
                tickers = self.ib.reqTickers(contract)
                if tickers:
                    t = tickers[0]
                    bid = t.bid
                    ask = t.ask
                    if (bid is None or bid <= 0 or ask is None or ask <= 0):
                        close = getattr(t, 'close', None)
                        last = getattr(t, 'last', None)
                        fallback = close if close and close > 0 else (last if last and last > 0 else None)
                        if fallback and fallback > 0:
                            bid = fallback
                            ask = fallback
                        else:
                            continue
                    quotes[symbol] = {
                        'symbol': symbol,
                        'midPrice': ((ask + bid) / 2) if ask and bid else float('nan'),
                        'bidPrice': bid,
                        'askPrice': ask,
                        'quoteTimeInLong': int(round(t.time.timestamp())) if t.time else None,
                    }
            except Exception as e:
                print(f"Error getting quote for {symbol}: {e}")
        return quotes

    def _convert_option_from_ibkr(self, ticker):
        date = ticker.lastTradeDateOrContractMonth
        year = date[2:4]
        month = date[4:6]
        day = date[6:]
        strike = int(ticker.strike) if ticker.strike == int(ticker.strike) else ticker.strike
        return ticker.symbol + "_" + month + day + year + ticker.right + str(strike)

    def _convert_option_to_ibkr(self, ticker):
        if "_" not in ticker:
            return ticker
        symb, option_part = ticker.split("_")
        date = option_part[2:4]
        month = option_part[:2]
        year = '20' + option_part[4:6]

        date = year + month + date
        right = (option_part[6])
        strike = float(option_part[7:])

        return self._ib.Option(symbol=symb, lastTradeDateOrContractMonth=date, \
                      strike=strike, right=right, multiplier='100', \
                      exchange='SMART', currency='USD')


def _test_ibkr():
    ibkr = IBKR()
    ok = ibkr.get_session()
    print(f"Connected: {ok}")
    if not ok:
        print("FAIL: Could not connect to IBKR")
        return
    inf = ibkr.get_account_info()
    print(f"Account balance: {inf.get('securitiesAccount',{}).get('currentBalances',{}).get('liquidationValue','N/A')}")

    test_symbols = ["SPY", "NVDA", "NVDA_052626C215", "SPY_061926C500"]
    for sym in test_symbols:
        print(f"\n--- Testing {sym} ---")
        cid = ibkr.get_con_id(sym)
        print(f"  conId: {cid}")
        if cid:
            q = ibkr.get_quotes([sym])
            print(f"  quotes: {q}")

    print("\n--- Testing make_BTO_lim_order ---")
    order = ibkr.make_BTO_lim_order("NVDA_052626C215", 1, 1.91)
    print(f"  order: {order}")

if __name__ == '__main__':
    _test_ibkr()
