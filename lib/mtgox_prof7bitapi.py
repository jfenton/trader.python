"""Mt.Gox API"""

#  Copyright (c) 2013 Bernd Kreuss <prof7bit@gmail.com>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

# pylint: disable=C0302,C0301,R0902,R0903,R0912,R0913,W0703

import sys
PY_VERSION = sys.version_info

if PY_VERSION < (2, 7):
    print("Sorry, minimal Python version is 2.7, you have: %d.%d"
        % (PY_VERSION.major, PY_VERSION.minor))
    sys.exit(1)

from ConfigParser import SafeConfigParser
import base64
import binascii
import contextlib
from Crypto.Cipher import AES
import getpass
import gzip
import hashlib
import hmac
import inspect
import io
import json
import logging
import Queue
import socket
import ssl
import time
import traceback
import threading
import urllib
import urllib2
import weakref
import websocket

import unlock_api_key

input = raw_input # pylint: disable=W0622,C0103

FORCE_PROTOCOL = "socketio"
FORCE_NO_FULLDEPTH = False
FORCE_NO_HISTORY = False
FORCE_HTTP_API = False

def int2str(value_int, currency):
    """return currency integer formatted as a string"""
    if currency == "BTC":
        return ("%16.8f" % (value_int / 1E8))
    if currency == "JPY":
        return ("%12.3f" % (value_int / 1E3))
    if currency == "USD":
        return ("%12.5f" % (value_int / 1E5))
    else:
        return ("%12.5f" % (value_int / 1E5))

def int2float(value_int, currency):
    """convert integer to float, determine the factor by currency name"""
    if currency == "BTC":
        return value_int / 1E8
    if currency == "JPY":
        return value_int / 1E3
    if currency == "USD":
        return value_int / 1E5
    else:
        return value_int / 1E5

def float2int(value_float, currency):
    """convert float value to integer, determine the factor by currency name"""
    if currency == "BTC":
        return int(value_float * 1E8)
    if currency == "JPY":
        return int(value_float * 1E3)
    if currency == "USD":
        return int(value_float * 1E5)
    else:
        return int(value_float * 1E5)

def http_request(url):
    """request data from the HTTP API, returns a string"""
    request = urllib2.Request(url)
    request.add_header('Accept-encoding', 'gzip')
    data = ""
    try:
        with contextlib.closing(urllib2.urlopen(request)) as response:
            if response.info().get('Content-Encoding') == 'gzip':
                with io.BytesIO(response.read()) as buf:
                    with gzip.GzipFile(fileobj=buf) as unzipped:
                        data = unzipped.read()
            else:
                data = response.read()
        return data
    #Try to catch a number of possible errors. 
    except urllib2.HTTPError as e:
        #HTTP Error ie: 500/502/503 etc
        logging.debug('HTTP Error %s: %s' % (e.code, e.msg))
        logging.debug("URL: %s" % (e.filename))
        if e.fp:
            datastring = e.fp.read()
            if "error" in datastring:
                if "<!DOCTYPE HTML>" in datastring:
                    logging.debug("Error: Cloudflare - Website Currently Unavailable.")
                elif "Order not found" in datastring:
                    return json.loads(datastring)
                else:
                    logging.debug("Error: %s" % datastring)
    except urllib2.URLError as e:
        logging.debug("URL Error:", e)
    except ssl.SSLError as e:
        logging.debug("SSL Error: %s." % e)  #Read error timeout. (Removed timeout variable)
    except Exception as e:
        logging.debug("General Error: %s" % e)


def start_thread(thread_func):
    """start a new thread to execute the supplied function"""
    thread = threading.Thread(target=thread_func)
    thread.daemon = True
    thread.start()
    return thread

def pretty_format(something):
    """pretty-format a nested dict or list for debugging purposes.
    If it happens to be a valid json string then it will be parsed first"""
    try:
        return pretty_format(json.loads(something))
    except Exception:
        try:
            return json.dumps(something, indent=5)
        except Exception:
            return str(something)


# pylint: disable=R0904
class GoxConfig(SafeConfigParser):
    """return a config parser object with default values. If you need to run
    more Gox() objects at the same time you will also need to give each of them
    them a separate GoxConfig() object. For this reason it takes a filename
    in its constructor for the ini file, you can have separate configurations
    for separate Gox() instances"""

    _DEFAULTS = [["gox", "currency", "USD"]
                ,["gox", "use_ssl", "True"]
                ,["gox", "use_plain_old_websocket", "False"]
                ,["gox", "use_http_api", "True"]
                ,["gox", "load_fulldepth", "True"]
                ,["gox", "load_history", "True"]
                ,["goxtool", "set_xterm_title", "True"]
                ]

    def __init__(self): 
        SafeConfigParser.__init__(self)
        for (sect, opt, default) in self._DEFAULTS:
            self._default(sect, opt, default)


    def get_safe(self, sect, opt):
        """get value without throwing exception."""
        try:
            return self.get(sect, opt)
        # pylint: disable=W0702
        except:
            for (dsect, dopt, default) in self._DEFAULTS:
                if dsect == sect and dopt == opt:
                    self._default(sect, opt, default)
                    return default
            return ""

    def get_bool(self, sect, opt):
        """get boolean value from config"""
        return self.get_safe(sect, opt) == "True"

    def get_string(self, sect, opt):
        """get string value from config"""
        return self.get_safe(sect, opt)

    def _default(self, section, option, default):
        """create a default option if it does not yet exist"""
        if not self.has_section(section):
            self.add_section(section)
        if not self.has_option(section, option):
            self.set(section, option, default)


class Signal():
    """callback functions (so called slots) can be connected to a signal and
    will be called when the signal is called (Signal implements __call__).
    The slots receive two arguments: the sender of the signal and a custom
    data object. Two different threads won't be allowed to send signals at the
    same time application-wide, concurrent threads will have to wait until
    the lock is releaesed again. The lock allows recursive reentry of the same
    thread to avoid deadlocks when a slot wants to send a signal itself."""

    _lock = threading.RLock()
    signal_error = None

    def __init__(self):
        self._functions = weakref.WeakSet()
        self._methods = weakref.WeakKeyDictionary()

        # the Signal class itself has a static member signal_error where it
        # will send tracebacks of exceptions that might happen. Here we
        # initialize it if it does not exist already
        if not Signal.signal_error:
            Signal.signal_error = 1
            Signal.signal_error = Signal()

    def connect(self, slot):
        """connect a slot to this signal. The parameter slot can be a funtion
        that takes exactly 2 arguments or a method that takes self plus 2 more
        arguments, or it can even be even another signal. the first argument
        is a reference to the sender of the signal and the second argument is
        the payload. The payload can be anything, it totally depends on the
        sender and type of the signal."""
        if inspect.ismethod(slot):
            if slot.__self__ not in self._methods:
                self._methods[slot.__self__] = set()
            self._methods[slot.__self__].add(slot.__func__)
        else:
            self._functions.add(slot)

    def __call__(self, sender, data, error_signal_on_error=True):
        """dispatch signal to all connected slots. This is a synchronuos
        operation, It will not return before all slots have been called.
        Also only exactly one thread is allowed to emit signals at any time,
        all other threads that try to emit *any* signal anywhere in the
        application at the same time will be blocked until the lock is released
        again. The lock will allow recursive reentry of the seme thread, this
        means a slot can itself emit other signals before it returns (or
        signals can be directly connected to other signals) without problems.
        If a slot raises an exception a traceback will be sent to the static
        Signal.signal_error() or to logging.critical()"""
        with self._lock:
            sent = False
            errors = []
            for func in self._functions:
                try:
                    func(sender, data)
                    sent = True

                # pylint: disable=W0702
                except:
                    errors.append(traceback.format_exc())

            for obj, funcs in self._methods.items():
                for func in funcs:
                    try:
                        func(obj, sender, data)
                        sent = True

                    # pylint: disable=W0702
                    except:
                        errors.append(traceback.format_exc())

            for error in errors:
                if error_signal_on_error:
                    Signal.signal_error(self, (error), False)
                else:
                    logging.critical("###Error:",error)

            return sent


class BaseObject():
    """This base class only exists because of the debug() method that is used
    in many of the goxtool objects to send debug output to the signal_debug."""

    def __init__(self):
        self.signal_debug = Signal()

    def debug(self, *args):
        """send a string composed of all *args to all slots who
        are connected to signal_debug or send it to the logger if
        nobody is connected"""
        msg = " ".join([str(x) for x in args])
        if not self.signal_debug(self, (msg)):
            logging.debug(msg)
        #print msg


class Timer(Signal):
    """a simple timer (used for stuff like keepalive)"""

    def __init__(self, interval):
        """create a new timer, interval is in seconds"""
        Signal.__init__(self)
        self._interval = interval
        self._timer = None
        self._start()

    def _fire(self):
        """fire the signal and restart it"""
        self.__call__(self, None)
        self._start()

    def _start(self):
        """start the timer"""
        self._timer = threading.Timer(self._interval, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        """cancel the timer"""
        self._timer.cancel()


class Secret:
    """Manage the MtGox API secret. This class has methods to decrypt the
    entries in the file and it also provides a method to create these
    entries. The methods encrypt() and decrypt() will block and ask
    questions on the command line, they are called outside the curses
    environment (yes, its a quick and dirty hack but it works for now)."""

    def __init__(self):
        """initialize the instance"""
        self.key = ""
        self.secret = ""

    def decrypt(self, password=""):
        """decrypt "secret_secret" from the file with the given password.
        This will return false if decryption did not seem to be successful.
        After this menthod succeeded the application can access the secret"""
        if self.know_secret():
            return 1
        try:
            self.key,self.secret, _ = unlock_api_key.unlock("mtgox",password)
            return 1
        except:
            return False
        
    def prompt_decrypt(self,password=""):
        """ask the user for password on the command line
        and then try to decrypt the secret."""
        self.decrypt(password)


    def know_secret(self):
        """do we know the secret key? The application must be able to work
        without secret and then just don't do any account related stuff"""
        return(self.secret != "") and (self.key != "")


class OHLCV():
    """represents a chart candle. tim is POSIX timestamp of open time,
    prices and volume are integers like in the other parts of the gox API"""

    def __init__(self, tim, opn, hig, low, cls, vol):
        self.tim = tim
        self.opn = opn
        self.hig = hig
        self.low = low
        self.cls = cls
        self.vol = vol

    def update(self, price, volume):
        """update high, low and close values and add to volume"""
        if price > self.hig:
            self.hig = price
        if price < self.low:
            self.low = price
        self.cls = price
        self.vol += volume


class History(BaseObject):
    """represents the trading history"""

    def __init__(self, gox, timeframe):
        BaseObject.__init__(self)

        self.signal_changed = Signal()

        
        self.candles = []
        self.timeframe = timeframe

        gox.signal_trade.connect(self.slot_trade)
        gox.signal_fullhistory.connect(self.slot_fullhistory)

    def add_candle(self, candle):
        """add a new candle to the history"""
        self._add_candle(candle)
        self.signal_changed(self, (self.length()))

    def slot_trade(self, dummy_sender, data):
        """slot for gox.signal_trade"""
        (date, price, volume, dummy_typ, own) = data
        if not own:
            time_round = int(date / self.timeframe) * self.timeframe
            candle = self.last_candle()
            if candle:
                if candle.tim == time_round:
                    candle.update(price, volume)
                    self.signal_changed(self, (1))
                else:
                    self.debug("### opening new candle")
                    self.add_candle(OHLCV(
                        time_round, price, price, price, price, volume))
            else:
                self.add_candle(OHLCV(
                    time_round, price, price, price, price, volume))

    def _add_candle(self, candle):
        """add a new candle to the history but don't fire signal_changed"""
        self.candles.insert(0, candle)

    def slot_fullhistory(self, dummy_sender, data):
        """process the result of the fullhistory request"""
        (history) = data
        self.candles = []
        new_candle = OHLCV(0, 0, 0, 0, 0, 0)
        for trade in history:
            date = int(trade["date"])
            price = int(trade["price_int"])
            volume = int(trade["amount_int"])
            time_round = int(date / self.timeframe) * self.timeframe
            if time_round > new_candle.tim:
                if new_candle.tim > 0:
                    self._add_candle(new_candle)
                new_candle = OHLCV(
                    time_round, price, price, price, price, volume)
            new_candle.update(price, volume)

        # insert current (incomplete) candle
        self._add_candle(new_candle)
        self.debug("### got %d candles" % self.length())
        self.signal_changed(self, (self.length()))

    def last_candle(self):
        """return the last (current) candle or None if empty"""
        if self.length() > 0:
            return self.candles[0]
        else:
            return None

    def length(self):
        """return the number of candles in the history"""
        return len(self.candles)


class BaseClient(BaseObject):
    """abstract base class for SocketIOClient and WebsocketClient"""

    SOCKETIO_HOST = "socketio.mtgox.com"
    WEBSOCKET_HOST = "websocket.mtgox.com"
    HTTP_HOST = "data.mtgox.com"

    _last_nonce = 0
    _nonce_lock = threading.Lock()

    def __init__(self, gox, secret, config):
        BaseObject.__init__(self)

        self.signal_recv        = Signal()
        self.signal_fulldepth   = Signal()
        self.signal_fullhistory = Signal()

        self.signal_backupticker = Signal()
        self._keepalive_timer = Timer(60)
        
        self.currency = gox.currency
        self.gox = gox
        self.secret = secret
        self.config = config

        self.http_requests = Queue.Queue()
        self.socket = None
        self.connected = False
        self.created = 0
        self._terminate = threading.Event()
        self._terminate.set()
        self._time_last_received = 0
        
    def start(self):
        """start the client"""
        self._terminate.clear()
        if not self.connected:
            self.debug("Starting Client, currency=" + self.currency)
            self._recv_thread = start_thread(self._recv_thread_func)
            self._http_thread = start_thread(self._http_thread_func)

    def stop(self):
        """stop the client"""
        self._terminate.set()
        if self.connected:
            self.debug("Shutting down client & closing socket")
            self.socket.close()
            self.connected = False
            self.socket = None

    def _try_send_raw(self, raw_data):
        """send raw data to the websocket or disconnect and close"""
        if self.connected:
            try:
                self.socket.send(raw_data)
            except Exception as exc:
                self.debug(exc)
                self.socket.close()

    # def send(self, json_str):
    #     """there exist 2 subtly different ways to send a string over a
    #     websocket. Each client class will override this send method"""
    #     raise NotImplementedError()

    def get_nonce(self):
        """produce a unique nonce that is guaranteed to be ever increasing"""
        with self._nonce_lock:
            nonce = int(time.time()*1000)
            if nonce <= self._last_nonce:
                nonce = self._last_nonce + 1
            self._last_nonce = nonce
            return nonce

    def request_order_lag(self):
        """request the current order-lag"""
        if FORCE_HTTP_API or self.config.get_bool("gox", "use_http_api"):
            self.enqueue_http_request("money/order/lag", {}, "order_lag")
        else:
            self.send_signed_call("order/lag", {}, "order_lag")

    def request_fulldepth(self):
        """start the fulldepth thread"""

        def fulldepth_thread():
            """request the full market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            try:
                fdtdelta = time.time() - self.gox.orderbook.fulldepth_time
                self.debug("### Requesting /api/2/BTC" + self.currency + "/money/depth/full. Updated %.3f ago" % fdtdelta)
                fulldepth = http_request("https://" +  self.HTTP_HOST \
                    + "/api/2/BTC" + self.currency + "/money/depth/full")
                self.signal_fulldepth(self, (json.loads(fulldepth)))
            except Exception as e:
                self.debug("###request_fulldepth: Error:",e)

        start_thread(fulldepth_thread)


    def request_fetchdepth(self):
        """start the fetchdepth thread"""

        def fetchdepth_thread():
            """request the partial market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            try:
                fdtdelta = time.time() - self.gox.orderbook.fulldepth_time
                self.debug("### Requesting /api/2/BTC" + self.currency + "/money/depth/fetch. Updated %.3f ago" % fdtdelta)
                fulldepth = http_request("https://" +  self.HTTP_HOST \
                    + "/api/2/BTC" + self.currency + "/money/depth/fetch")
                self.signal_fulldepth(self, (json.loads(fulldepth)))
            except Exception as e:
                self.debug("###request_fetchdepth: Error:",e)

        start_thread(fetchdepth_thread)

    def request_history(self):
        """request 24h trading history"""

        def history_thread():
            try:
                """request trading history"""
                self.debug("Requesting /api/2/BTC" + self.currency + "/money/trades")
                json_hist = http_request("https://" +  self.HTTP_HOST \
                    + "/api/2/BTC" + self.currency + "/money/trades")
                history = json.loads(json_hist)
                if history["result"] == "success":
                    self.signal_fullhistory(self, history["data"])
            except:
                self.debug("###request_history: Error:",e)

        start_thread(history_thread)

    def request_ticker(self):
        """request ticker using API 0 - most accurate."""
        def ticker_thread():
            try:
                """request ticker"""
                self.debug("Requesting /api/2/" + self.currency + "/money/ticker_fast")
                json_ticker = http_request("https://" +  self.HTTP_HOST \
                    + "/api/2/BTC" + self.currency + "/money/ticker_fast" )
                ticker = json.loads(json_ticker)["data"]
                data = (float2int(ticker["buy"]["value"],self.currency), \
                    float2int(ticker["sell"]["value"],self.currency))
                self.signal_backupticker(self,data)
            except:
                self.debug("###request_ticker: Error:",e)

        start_thread(ticker_thread)

    def request_getdepthapi0(self):
        """request getDepth using API 0 - fastest"""
        def getdepth_thread():
            try:
                """request getDepth api 0"""
                self.debug("Requesting /api/0/getDepth.php")
                json_smalldepth = http_request("https://" +  self.HTTP_HOST \
                    + "/api/0/data/getDepth.php?Currency=" + self.currency)
                smalldepth = json.loads(json_smalldepth)
                bids = smalldepth["bids"]
                smalldepthmaindict = {}
                newbids = []
                for bid in bids:
                    eachbid = {}
                    eachbid["price_int"] = float2int(bid[0],self.currency)
                    eachbid["amount_int"] = float2int(bid[1],"BTC")
                    newbids.append(eachbid)
                asks = smalldepth["asks"]
                newasks = []
                for ask in asks:
                    eachask = {}
                    eachask["price_int"] = float2int(ask[0],self.currency)
                    eachask["amount_int"] = float2int(ask[1],"BTC")
                    newasks.append(eachask)
                smalldepthmaindict["data"] = {}
                smalldepthmaindict["data"]["bids"]=newbids
                smalldepthmaindict["data"]["asks"]=newasks
                self.signal_fulldepth(self, smalldepthmaindict)
            except:
                self.debug("###request_getdepthapi0: Error:",e)

        start_thread(getdepth_thread)        

    # def _recv_thread_func(self):
    #     """this will be executed as the main receiving thread, each type of
    #     client (websocket or socketio) will implement its own"""
    #     raise NotImplementedError()

    def channel_subscribe(self):
        """subscribe to the needed channels and alo initiate the
        download of the initial full market depth"""
        # Once you join 1::/mtgox these are automaticlaly subscribed to
        # CHANNELS = 
        #     "dbf1dee9-4f2e-4a08-8cb7-748919a71b21": "trades",
        #     "d5f06780-30a8-4a48-a2f8-7ed181b4a13f": "ticker",
        #     "24e67e0d-1cad-4cc0-9e7a-f8523ef460fe": "depth",
        #self.send(json.dumps({"op":"mtgox.subscribe", "type":"depth"}))
        #self.send(json.dumps({"op":"mtgox.subscribe", "type":"ticker"}))
        #self.send(json.dumps({"op":"mtgox.subscribe", "type":"trades"}))
        #This lag one is not automatic.        
        #self.send(json.dumps({"op":"mtgox.subscribe", "type":"lag"}))

        #if self.gox.client.connected == True and self.gox.client_backup.connected == True:
        if self.gox.client.connected == True or self.gox.client_backup.connected == True:
            if not(self.gox._idkey):
                if FORCE_HTTP_API or self.config.get_bool("gox", "use_http_api"):
                    self.enqueue_http_request("money/idkey", {}, "idkey")
            else:
                self.debug("### already have idkey, subscribing to account messages:")
                self.gox.client.send(json.dumps({"op":"mtgox.subscribe", "key":self.gox._idkey}))
                    #self.debug("Calling HTTP API's for: orders/idkey/info")
                    #self.enqueue_http_request("money/orders", {}, "orders")
                    #self.enqueue_http_request("money/info", {}, "info")
                #else:
                    #self.debug("Sending Socket messages requesting: orders/idkey/info")
                    #self.send_signed_call("private/orders", {}, "orders")
                    #self.send_signed_call("private/idkey", {}, "idkey")
                    #self.send_signed_call("private/info", {}, "info")

            # if self.config.get_bool("gox", "load_history"):
            #     if not FORCE_NO_HISTORY:
            #         self.request_history()

            fdtdelta = time.time() - self.gox.orderbook.fulldepth_time
            if fdtdelta > 120:

                if self.config.get_bool("gox", "load_fulldepth"):
                    if not FORCE_NO_FULLDEPTH:
                        self.request_fulldepth()

            elif fdtdelta > 15:
                self.request_fetchdepth()


    def _http_thread_func(self):
        """send queued http requests to the http API (only used when
        http api is forced, normally this is much slower)"""
        while not(self._terminate.isSet()):
            (api_endpoint, params, reqid) = self.http_requests.get(True)
            try:
                success = False
                while success == False:
                    answer = self.http_signed_call(api_endpoint, params)
                    if answer["result"] == "success":
                        # the fiollowing will reformat the answer in such a way
                        # that we can pass it directly to signal_recv()
                        # as if it had come directly from the websocket
                        ret = {"op": "result", "id": reqid, "result": answer["data"]}
                        self.signal_recv(self, (json.dumps(ret)))
                        success = True
                    else:
                        self.debug("### Error,retrying...:", answer, reqid)                
            except Exception as exc:
                self.debug("### Error,failure:", exc, api_endpoint, params, reqid)
                
            self.http_requests.task_done()

    def enqueue_http_request(self, api_endpoint, params, reqid):
        """enqueue a request for sending to the HTTP API, returns
        immediately, behaves exactly like sending it over the websocket."""
        if self.secret and self.secret.know_secret():
            self.http_requests.put((api_endpoint, params, reqid))

    def http_signed_call(self, api_endpoint, params):
        """send a signed request to the HTTP API V2"""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        params["nonce"] = self.get_nonce()
        post = urllib.urlencode(params)
        prefix = api_endpoint + chr(0)
        # pylint: disable=E1101
        sign = hmac.new(base64.b64decode(sec), prefix + post, hashlib.sha512).digest()

        headers = {
            'User-Agent': 'genBTC-bot',
            'Rest-Key': key,
            'Rest-Sign': base64.b64encode(sign)
        }

        url = "https://" + self.HTTP_HOST + "/api/2/" + api_endpoint
        self.debug("### (http) calling %s" % url)
        req = urllib2.Request(url, post, headers)
        with contextlib.closing(urllib2.urlopen(req, post)) as res:
            return json.load(res)


    def send_signed_call(self, api_endpoint, params, reqid):
        """send a signed (authenticated) API call over the socket.io.
        This method will only succeed if the secret key is available,
        otherwise it will just log a warning and do nothing."""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        nonce = self.get_nonce()

        call = json.dumps({
            "id"       : reqid,
            "call"     : api_endpoint,
            "nonce"    : nonce,
            "params"   : params,
            "currency" : self.currency,
            "item"     : "BTC"
        })

        # pylint: disable=E1101
        sign = hmac.new(base64.b64decode(sec), call, hashlib.sha512).digest()
        signedcall = key.replace("-", "").decode("hex") + sign + call

        self.debug("### (socket) calling %s" % api_endpoint)
        self.send(json.dumps({
            "op"      : "call",
            "call"    : base64.b64encode(signedcall),
            "id"      : reqid,
            "context" : "mtgox.com"
        }))

    def send_order_add(self, typ, price, volume):
        """send an order"""
        reqid = "order_add:%s:%d:%d" % (typ, price, volume)
        if FORCE_HTTP_API or self.config.get_bool("gox", "use_http_api"):
            api = "BTC%s/money/order/add" % self.currency
            params = {"type": typ, "price_int": price, "amount_int": volume}
            self.enqueue_http_request(api, params, reqid)
        else:
            api = "order/add"
            params = {"type": typ, "price_int": price, "amount_int": volume}
            self.send_signed_call(api, params, reqid)

    def send_order_cancel(self, oid):
        """cancel an order"""
        params = {"oid": oid}
        reqid = "order_cancel:%s" % oid
        if FORCE_HTTP_API or self.config.get_bool("gox", "use_http_api"):
            api = "money/order/cancel"
            self.enqueue_http_request(api, params, reqid)
        else:
            api = "order/cancel"
            self.send_signed_call(api, params, reqid)



class WebsocketClient(BaseClient):
    """this implements a connection to MtGox through the older (but faster)
    websocket protocol. Unfortuntely its just as unreliable as the socket.io."""

    def __init__(self, gox, secret, config):
        BaseClient.__init__(self, gox, secret, config)

    def _recv_thread_func(self):
        """connect to the webocket and start receiving in an infinite loop.
        Try to reconnect whenever connection is lost. Each received json
        string will be dispatched with a signal_recv signal"""
        reconnect_time = 0
        use_ssl = self.config.get_bool("gox", "use_ssl")
        wsp = {True: "wss://", False: "ws://"}[use_ssl]
        while not(self._terminate.is_set()):  #loop 0 (connect, reconnect)
            try:
                self._terminate.wait(reconnect_time)
                reconnect_time = 20
                ws_url = wsp + self.WEBSOCKET_HOST + "/mtgox?Currency=" + self.gox.currency

                self.debug("trying plain old Websocket: %s" % ws_url)

                self.socket = websocket.WebSocket()
                self.socket.connect(ws_url)
                if self.socket.connected:
                    self.debug("connected.")
                    self.connected = True
                    self.created = time.time()
                self.channel_subscribe()
                
                self.debug("waiting for data...")
                while not self._terminate.is_set(): #loop1 (read messages)
                    str_json = self.socket.recv()
                    if str_json[0] == "{":
                        self._time_last_received = time.time()
                        self.signal_recv(self, (str_json))

            except Exception as exc:
                self.connected = False
                self._terminate.set()
                self.debug(exc, "\n"+("\t"*6)+"Reconnecting to WebSocket in %i seconds..." % reconnect_time)

    def send(self, json_str):
        """send the json encoded string over the websocket"""
        self._try_send_raw(json_str)
       


class SocketIO(websocket.WebSocket):
    """This is the WebSocket() class with added Super Cow Powers. It has a
    different connect method so that it can connect to socket.io. It will do
    the initial HTTP request with keep-alive and then use that same socket
    to upgrade to websocket"""
    def __init__(self, get_mask_key = None):
        websocket.WebSocket.__init__(self, get_mask_key)

    def connect(self, url, **options):
        """connect to socketio and then upgrade to websocket transport. Example:
        connect('wss://websocket.mtgox.com/socket.io/1', query='Currency=EUR')"""
        def read_block(sock):
            """read from the socket until empty line, return list of lines"""
            lines = []
            line = ""
            while True:
                res = sock.recv(1)
                line += res
                if res == "":
                    return None
                if res == "\n":
                    line = line.strip()
                    if line == "":
                        return lines
                    lines.append(line)
                    line = ""

        # pylint: disable=W0212
        hostname, port, resource, is_secure = websocket._parse_url(url)
        self.sock.connect((hostname, port))
        if is_secure:
            self.io_sock = websocket._SSLSocketWrapper(self.sock)

        path_a = resource
        if "query" in options:
            path_a += "?" + options["query"]
        self.io_sock.send("GET %s HTTP/1.1\r\n" % path_a)
        self.io_sock.send("Host: %s:%d\r\n" % (hostname, port))
        self.io_sock.send("User-Agent: genBTC-bot\r\n")
        self.io_sock.send("Accept: text/plain\r\n")
        self.io_sock.send("Connection: keep-alive\r\n")
        self.io_sock.send("\r\n")

        headers = read_block(self.io_sock)
        if not headers:
            raise IOError("disconnected while reading headers")
        if not "200" in headers[0]:
            raise IOError("wrong answer: %s" % headers[0])
        result = read_block(self.io_sock)
        if not result:
            raise IOError("disconnected while reading socketio session ID")
        if len(result) != 3:
            raise IOError("invalid response from socket.io server")

        ws_id = result[1].split(":")[0]
        resource += "/websocket/" + ws_id
        if "query" in options:
            resource += "?" + options["query"]

        self._handshake(hostname, port, resource, **options)



class SocketIOClient(BaseClient):
    """this implements a connection to MtGox using the new socketIO protocol.
    This should replace the older plain websocket API"""

    def __init__(self, gox, secret, config):
        BaseClient.__init__(self, gox, secret, config)
        self.hostname = self.SOCKETIO_HOST
        self._keepalive_timer.connect(self.slot_keepalive_timer)


    def _recv_thread_func(self):
        """this is the main thread that is running all the time. It will
        connect and then read (blocking) on the socket in an infinite
        loop. SocketIO messages ('2::', etc.) are handled here immediately
        and all received json strings are dispathed with signal_recv."""
        use_ssl = self.config.get_bool("gox", "use_ssl")
        wsp = {True: "wss://", False: "ws://"}[use_ssl]
        reconnect_time = 0
        while not(self._terminate.is_set()): #loop 0 (connect, reconnect)
            try:
                self._terminate.wait(reconnect_time)
                reconnect_time = 1
                ws_url = wsp + self.hostname + "/socket.io/1"

                self.debug("trying Socket.IO: %s" % ws_url)
                self.socket = SocketIO()
                self.socket.connect(ws_url, query="Currency=" + self.gox.currency)

                if self.socket.connected:
                    self.debug("connected.")
                    self.connected = True
                    self.created = time.time()
                
                self.channel_subscribe()
                self.socket.send("1::/mtgox")
                #self.send(json.dumps({"op":"unsubscribe", "channel":"24e67e0d-1cad-4cc0-9e7a-f8523ef460fe"}))
                #self.send(json.dumps({"op":"unsubscribe", "channel":"d5f06780-30a8-4a48-a2f8-7ed181b4a13f"}))

                #self.debug(self.socket.recv())
                #self.debug(self.socket.recv())
               
                self.debug("waiting for data...")
                while not self._terminate.is_set(): #loop1 (read messages)
                    msg = self.socket.recv()
                    if msg == "2::":
                        self.socket.send("2::")
                        continue
                    prefix = msg[:10]
                    if prefix == "4::/mtgox:":
                        str_json = msg[10:]
                        if str_json[0] == "{":
                            self._time_last_received = time.time()
                            self.signal_recv(self, (str_json))

            except Exception as exc:
                self.connected = False
                self.debug(exc.__class__.__name__, exc, "reconnecting to SocketIO...")
                self.gox.client_backup.start()


    def send(self, json_str):
        """send a string to the websocket. This method will prepend it
        with the 1::/mtgox: that is needed for the socket.io protocol
        (as opposed to plain websockts) and the underlying websocket
        will then do the needed framing on top of that."""
        self._try_send_raw("4::/mtgox:" + json_str)

    def slot_keepalive_timer(self, _sender, _data):
        """send a keepalive, just to make sure our socket is not dead"""
        self._try_send_raw("2::")
        self.request_order_lag()



class SocketIOBetaClient(SocketIOClient):
    """experimental client for the beta websocket"""
    def __init__(self, currency, secret, config):
        SocketIOClient.__init__(self, currency, secret, config)
        self.hostname = self.SOCKETIO_HOST_BETA



# pylint: disable=R0902
class Gox(BaseObject):
    """represents the API of the MtGox exchange. An Instance of this
    class will connect to the streaming socket.io API, receive live
    events, it will emit signals you can hook into for all events,
    it has methods to buy and sell"""

    def __init__(self, secret, config):
        """initialize the gox API but do not yet connect to it."""
        BaseObject.__init__(self)

        self.signal_depth           = Signal()
        self.signal_trade           = Signal()
        self.signal_ticker          = Signal()
        self.signal_fulldepth       = Signal()
        self.signal_fullhistory     = Signal()
        self.signal_wallet          = Signal()
        self.signal_userorder       = Signal()
        self.signal_orderlag        = Signal()

        # the following are not fired by gox itself but by the
        # application controlling it to pass some of its events
        self.signal_keypress        = Signal()
        self.signal_strategy_unload = Signal()

        self._idkey = None
        self.wallet = {}
        self.order_lag = 0
#added        
        self._time_last_received = 0
        self.LASTTICKER = time.time() - 20
        self.LASTLAG = time.time() - 20  

        self.config = config
        self.currency = config.get("gox", "currency", "USD")

        Signal.signal_error.connect(self.signal_debug)

        self.history = History(self, 60 * 15)
        self.history.signal_debug.connect(self.signal_debug)


        self.client = SocketIOClient(self, secret, config)
#added        
        self.client_backup = WebsocketClient(self, secret, config)
#moved
        self.orderbook = OrderBook(self)
        self.orderbook.signal_debug.connect(self.signal_debug)

        self.client.signal_debug.connect(self.signal_debug)
        self.client.signal_recv.connect(self.slot_recv)
#added
        self.client_backup.signal_debug.connect(self.signal_debug)
        self.client_backup.signal_recv.connect(self.slot_recv)

        self.client.signal_fulldepth.connect(self.signal_fulldepth)
        self.client.signal_fullhistory.connect(self.signal_fullhistory)
##New
        self._switchclient = Timer(15)
        self._switchclient.connect(self.slot_switchclient)

##Code to switch between SocketIO/websocket/HTTP ticker
    def slot_switchclient(self, _sender, _data):
        """find out if the socket is blank in regular intervals, and if it is, request new HTTP depth"""

        silent = time.time() - self.client._time_last_received
        if silent > 60:
            if time.time() - self.client.created > 60:
                self.debug("NO DATA received over SocketIO for %d seconds!!!!!! Restarting SocketIO Client" % silent)
                self.stop()
                time.sleep(2)
                self.start()
                if self.client_backup._terminate.isSet() and not self.client_backup.connected:
                    self.debug("SocketIO is NOT sending data. Starting WebSocket client.")
                    self.client_backup.start()
            if time.time() - self.orderbook.fulldepth_time > 20 and not(self.client_backup.connected):
                self.client.request_fetchdepth()
               
        elif silent <= 60 and not(self.client_backup._terminate.isSet()):
            self.debug("SocketIO is actively sending data. Stopping WebSocket client.")
            self.client_backup.stop()


    def start(self):
        """connect to MtGox and start receiving events."""
        self.client.start()

    def stop(self):
        """shutdown the client"""
        self.client.stop()
        
    def order(self, typ, price, volume):
        """place pending order. If price=0 then it will be filled at market"""
        self.client.send_order_add(typ, price, volume)

    def buy(self, price, volume):
        """new buy order, if price=0 then buy at market"""
        self.order("bid", price, volume)

    def sell(self, price, volume):
        """new sell order, if price=0 then sell at market"""
        self.order("ask", price, volume)

    def cancel(self, oid):
        """cancel order"""
        self.client.send_order_cancel(oid)

    def cancel_by_price(self, price):
        """cancel all orders at price"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if order.price == price:
                if order.oid != "":
                    self.cancel(order.oid)

    def cancel_by_type(self, typ=None):
        """cancel all orders of type (or all orders if type=None)"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if typ == None or typ == order.typ:
                if order.oid != "":
                    self.cancel(order.oid)

    def slot_recv(self, dummy_sender, data):
        """Slot for signal_recv, handle new incoming JSON message. Decode the
        JSON string into a Python object and dispatch it to the method that
        can handle it."""
        (str_json) = data
        handler = None
        msg = json.loads(str_json)
        if "op" in msg:
            try:
                msg_op = msg["op"]
                handler = getattr(self, "_on_op_" + msg_op)

            except AttributeError:
                self.debug("slot_recv() ignoring: op=%s" % msg_op)
        else:
            self.debug("slot_recv() ignoring:", msg)

        if handler:
            handler(msg)

    def _on_op_error(self, msg):
        """handle error mesages (op:error)"""
        self.debug("_on_op_error()", msg)

    def _on_op_subscribe(self, msg):
        """handle subscribe messages (op:subscribe)"""
        self.debug("subscribed channel", msg["channel"])
        
    def _on_op_unsubscribe(self, msg):
        """handle unsubscribe messages (op:unsubscribe)"""
        self.debug("unsubscribed channel", msg["channel"])        

    def _on_op_result(self, msg):
        """handle result of authenticated API call (op:result, id:xxxxxx)"""
        result = msg["result"]
        reqid = msg["id"]

        if reqid == "idkey":
            self.debug("### got idkey, subscribing to account messages")
            self._idkey = result
            self.client.send(json.dumps({"op":"mtgox.subscribe", "key":result}))

        elif reqid == "orders":
            self.debug("### got own order list")
            self.orderbook.reset_own()
            for order in result:
                if order["currency"] == self.currency:
                    self.orderbook.add_own(Order(
                        int(order["price"]["value_int"]),
                        int(order["amount"]["value_int"]),
                        order["type"],
                        order["oid"],
                        order["status"]
                    ))
            self.debug("### have %d own orders for BTC/%s" %
                (len(self.orderbook.owns), self.currency))

        elif reqid == "info":
            self.debug("### got account info")
            gox_wallet = result["Wallets"]
            self.wallet = {}
            for currency in gox_wallet:
                self.wallet[currency] = int(
                    gox_wallet[currency]["Balance"]["value_int"])
            self.signal_wallet(self, ())

        elif reqid == "order_lag":
            lag_usec = result["lag"]
            lag_text = result["lag_text"]
            self.debug("### got order lag: %s" % lag_text)
            self.order_lag = lag_usec
            self.signal_orderlag(self, (lag_usec, lag_text))

        elif "order_add:" in reqid:
            # order/add has been acked and we got an oid, now we can already
            # insert a pending order into the owns list (it will be pending
            # for a while when the server is busy but the most important thing
            # is that we have the order-id already).
            parts = reqid.split(":")
            typ = parts[1]
            price = int(parts[2])
            volume = int(parts[3])
            oid = result
            self.debug("### got ack for order/add:", typ, price, volume, oid)
            self.orderbook.add_own(Order(price, volume, typ, oid, "pending"))

        elif "order_cancel:" in reqid:
            # cancel request has been acked but we won't remove it from our
            # own list now because it is still active on the server.
            # do nothing now, let things happen in the user_order message
            parts = reqid.split(":")
            oid = parts[1]
            self.debug("### got ack for order/cancel:", oid)

        else:
            self.debug("_on_op_result() ignoring:", msg)

    def _on_op_private(self, msg):
        """handle op=private messages, these are the messages of the channels
        we subscribed (trade, depth, ticker) and also the per-account messages
        (user_order, wallet, own trades, etc)"""
        private = msg["private"]
        handler = None
        try:
            handler = getattr(self, "_on_op_private_" + private)
        except AttributeError:
            self.debug("_on_op_private() ignoring: private=%s" % private)

        if handler:
            handler(msg)

    def _on_op_private_lag(self,msg):
        """handle incoming ticker message (op=private, private=lag)"""
        msg = msg["lag"]
        lag = str(float(msg["age"] / 1E6))
        self.debug(" lag: ",lag, "s")

    def _on_op_private_ticker(self, msg):
        """handle incoming ticker message (op=private, private=ticker)"""
        msg = msg["ticker"]
        if msg["sell"]["currency"] != self.currency:
            return
        ask = int(msg["sell"]["value_int"])
        bid = int(msg["buy"]["value_int"])

        now = float(msg["now"]) / 1E6
        if now - self.LASTTICKER > 20:    #only show the ticker every 20 seconds.
            self.LASTTICKER = now
            self.debug(" tick:  bid:", int2str(bid, self.currency),"   ask:", int2str(ask, self.currency))

        self.signal_ticker(self, (bid, ask))

    def _on_op_private_depth(self, msg):
        """handle incoming depth message (op=private, private=depth)"""
        msg = msg["depth"]
        if msg["currency"] != self.currency:
            return
        type_str = msg["type_str"]
        price = int(msg["price_int"])
        volume = int(msg["volume_int"])
        total_volume = int(msg["total_volume_int"])

#commented out to clean up log
#        self.debug(
#            "depth: ", type_str+":", int2str(price, self.currency),
#            "vol:", int2str(volume, "BTC"),
#            "total:", int2str(total_volume, "BTC"))
        self.signal_depth(self, (type_str, price, volume, total_volume))

    def _on_op_private_trade(self, msg):
        """handle incoming trade mesage (op=private, private=trade)"""
        if msg["trade"]["price_currency"] != self.currency:
            return
        if msg["channel"] == "dbf1dee9-4f2e-4a08-8cb7-748919a71b21":
            own = False
        else:
            own = True
        date = int(msg["trade"]["date"])
        price = int(msg["trade"]["price_int"])
        volume = int(msg["trade"]["amount_int"])
        typ = msg["trade"]["trade_type"]

        self.debug("TRADE: ", typ+":", int2str(price, self.currency),"\tvol:", int2str(volume, "BTC"))

        self.signal_trade(self, (date, price, volume, typ, own))

    def _on_op_private_user_order(self, msg):
        """handle incoming user_order message (op=private, private=user_order)"""
        order = msg["user_order"]
        oid = order["oid"]
        if "price" in order:
            if order["currency"] == self.currency:
                price = int(order["price"]["value_int"])
                volume = int(order["amount"]["value_int"])
                typ = order["type"]
                status = order["status"]
                self.signal_userorder(self,
                    (price, volume, typ, oid, status))

        else: # removed (filled or canceled)
            self.signal_userorder(self, (0, 0, "", oid, "removed"))

    def _on_op_private_wallet(self, msg):
        """handle incoming wallet message (op=private, private=wallet)"""
        balance = msg["wallet"]["balance"]
        currency = balance["currency"]
        total = int(balance["value_int"])
        self.wallet[currency] = total
        self.signal_wallet(self, ())

    def _on_op_remark(self, msg):
        """handler for op=remark messages"""

        if "success" in msg and not msg["success"]:
            if msg["message"] == "Invalid call":
                self._on_invalid_call(msg)
                return

        # we should log this, helps with debugging
        self.debug(msg)

    def _on_invalid_call(self, msg):
        """this comes as an op=remark message and is a strange mystery"""
        # Workaround: Maybe a bug in their server software,
        # I don't know whats missing. Its all poorly documented :-(
        # Sometimes some API calls fail the first time for no reason,
        # if this happens just send them again. This happens only
        # somtimes (10%) and sending them again will eventually succeed.

        if msg["id"] == "idkey":
            self.debug("### resending private/idkey")
            self.client.send_signed_call(
                "private/idkey", {}, "idkey")

        elif msg["id"] == "info":
            self.debug("### resending private/info")
            self.client.send_signed_call(
                "private/info", {}, "info")

        elif msg["id"] == "orders":
            self.debug("### resending private/orders")
            self.client.send_signed_call(
                "private/orders", {}, "orders")

        elif "order_add:" in msg["id"]:
            parts = msg["id"].split(":")
            typ = parts[1]
            price = int(parts[2])
            volume = int(parts[3])
            self.debug("### resending failed", msg["id"])
            self.client.send_order_add(typ, price, volume)

        elif "order_cancel:" in msg["id"]:
            parts = msg["id"].split(":")
            oid = parts[1]
            self.debug("### resending failed", msg["id"])
            self.client.send_order_cancel(oid)

        else:
            self.debug("_on_invalid_call() ignoring:", msg)


class Order:
    """represents an order in the orderbook"""

    def __init__(self, price, volume, typ, oid="", status=""):
        """initialize a new order object"""
        self.price = price
        self.volume = volume
        self.typ = typ
        self.oid = oid
        self.status = status


class OrderBook(BaseObject):
    """represents the orderbook. Each Gox instance has one
    instance of OrderBook to maintain the open orders. This also
    maintains a list of own orders belonging to this account"""

    def __init__(self, gox):
        """create a new empty orderbook and associate it with its
        Gox instance"""
        BaseObject.__init__(self)
        self.gox = gox

        self.signal_changed = Signal()
#added to delay startup of main program until its downloaded and this variable is True.
        self.fulldepth_downloaded = False
        self.fulldepth_time = 0

#added
        gox.client.signal_backupticker.connect(self.slot_ticker)

        gox.signal_ticker.connect(self.slot_ticker)
        gox.signal_depth.connect(self.slot_depth)
        gox.signal_trade.connect(self.slot_trade)
        gox.signal_userorder.connect(self.slot_user_order)
        gox.signal_fulldepth.connect(self.slot_fulldepth)

        self.bids = [] # list of Order(), highest bid first
        self.asks = [] # list of Order(), lowest ask first
        self.owns = [] # list of Order(), unordered list

        self.bid = 0
        self.ask = 0
        self.total_bid = 0
        self.total_ask = 0

    def slot_ticker(self, dummy_sender, data):
        """Slot for signal_ticker, incoming ticker message"""
        (bid, ask) = data
        self.bid = bid
        self.ask = ask
        self._repair_crossed_asks(ask)
        self._repair_crossed_bids(bid)
        self.signal_changed(self, ())

    def slot_depth(self, dummy_sender, data):
        """Slot for signal_depth, process incoming depth message"""
        (typ, price, _voldiff, total_vol) = data
        if typ == "ask":
            self._update_asks(price, total_vol)
        if typ == "bid":
            self._update_bids(price, total_vol)
        self.signal_changed(self, ())

    def slot_trade(self, dummy_sender, data):
        """Slot for signal_trade event, process incoming trade messages.
        For trades that also affect own orders this will be called twice:
        once during the normal public trade message, affecting the public
        bids and asks and then another time with own=True to update our
        own orders list"""
        (dummy_date, price, volume, typ, own) = data
        if own:
            self.debug("own order was filled")
            # nothing special to do here, there will also be
            # separate user_order messages to update my owns list

        else:
            voldiff = -volume
            if typ == "bid":  # trade_type=bid means an ask order was filled
                self._repair_crossed_asks(price)
                if len(self.asks):
                    if self.asks[0].price == price:
                        self.asks[0].volume -= volume
                        if self.asks[0].volume <= 0:
                            voldiff -= self.asks[0].volume
                            self.asks.pop(0)
                            self._update_total_ask(voldiff)
                if len(self.asks):
                    self.ask = self.asks[0].price

            if typ == "ask":  # trade_type=ask means a bid order was filled
                self._repair_crossed_bids(price)
                if len(self.bids):
                    if self.bids[0].price == price:
                        self.bids[0].volume -= volume
                        if self.bids[0].volume <= 0:
                            voldiff -= self.bids[0].volume
                            self.bids.pop(0)
                            self._update_total_bid(voldiff, price)
                if len(self.bids):
                    self.bid = self.bids[0].price

        self.signal_changed(self, ())

    def slot_user_order(self, dummy_sender, data):
        """Slot for signal_userorder, process incoming user_order mesage"""
        (price, volume, typ, oid, status) = data
        if status == "removed":
            for i in range(len(self.owns)):
                if self.owns[i].oid == oid:
                    order = self.owns[i]
                    self.debug(
                        "### removing %s order %s " % (order.typ,oid),
                        "price:", int2str(order.price, self.gox.currency),
                        "volume:", int2str(order.volume, "BTC"),
                        "type:", order.typ)
                    self.owns.pop(i)
                    break
        else:
            found = False
            for order in self.owns:
                if order.oid == oid:
                    found = True
                    order.price = price
                    order.volume = volume
                    order.typ = typ
                    order.oid = oid
                    if not(status == order.status):
                        self.debug(
                            "### updating %s order %s " % (typ,oid),
                            "price", int2str(price, self.gox.currency),
                            "volume:", int2str(volume, "BTC"),
                            "status:", status)
                        order.status = status
                    break

            if not found:
                self.debug(
                    "### adding %s order %s " % (typ,oid),
                    "price", int2str(price, self.gox.currency),
                    "volume:", int2str(volume, "BTC"),
                    "status:", status)
                self.owns.append(Order(price, volume, typ, oid, status))

        self.signal_changed(self, ())

    def slot_fulldepth(self, dummy_sender, data):
        """Slot for signal_fulldepth, process received fulldepth data.
        This will clear the book and then re-initialize it from scratch."""
        (depth) = data
        self.debug("### got full depth: updating orderbook...")
        self.bids = []
        self.asks = []
        self.total_ask = 0
        self.total_bid = 0
        if "error" in depth:
            self.debug("### ", depth["error"])
            return
        for order in depth["data"]["asks"]:
            price = int(order["price_int"])
            volume = int(order["amount_int"])
            self._update_total_ask(volume)
            self.asks.append(Order(price, volume, "ask"))
        for order in depth["data"]["bids"]:
            price = int(order["price_int"])
            volume = int(order["amount_int"])
            self._update_total_bid(volume, price)
            self.bids.insert(0, Order(price, volume, "bid"))

        self.bid = self.bids[0].price
        self.ask = self.asks[0].price
#added this        
        self.fulldepth_downloaded = True
        self.fulldepth_time = time.time()
        self.signal_changed(self, ())
        time.sleep(0.2)
        self.fulldepth_downloaded = False

    def _repair_crossed_bids(self, bid):
        """remove all bids that are higher that official current bid value,
        this should actually never be necessary if their feed would not
        eat depth- and trade-messages occaionally :-("""
        while len(self.bids) and self.bids[0].price > bid:
            price = self.bids[0].price
            volume = self.bids[0].volume
            self._update_total_bid(-volume, price)
            self.bids.pop(0)

    def _repair_crossed_asks(self, ask):
        """remove all asks that are lower that official current ask value,
        this should actually never be necessary if their feed would not
        eat depth- and trade-messages occaionally :-("""
        while len(self.asks) and self.asks[0].price < ask:
            volume = self.asks[0].volume
            self._update_total_ask(-volume)
            self.asks.pop(0)

    def _update_asks(self, price, total_vol):
        """update volume at this price level, remove entire level
        if empty after update, add new level if needed."""
        for i in range(len(self.asks)):
            level = self.asks[i]
            if level.price == price:
                # update existing level
                voldiff = total_vol - level.volume
                if total_vol == 0:
                    self.asks.pop(i)
                else:
                    level.volume = total_vol
                self._update_total_ask(voldiff)
                return
            if level.price > price and total_vol > 0:
                # insert before here and return
                lnew = Order(price, total_vol, "ask")
                self.asks.insert(i, lnew)
                self._update_total_ask(total_vol)
                return

        # still here? -> end of list or empty list.
        if total_vol > 0:
            lnew = Order(price, total_vol, "ask")
            self.asks.append(lnew)
            self._update_total_ask(total_vol)
#added this        
            self.ask = self.asks[0].price

    def _update_bids(self, price, total_vol):
        """update volume at this price level, remove entire level
        if empty after update, add new level if needed."""
        for i in range(len(self.bids)):
            level = self.bids[i]
            if level.price == price:
                # update existing level
                voldiff = total_vol - level.volume
                if total_vol == 0:
                    self.bids.pop(i)
                else:
                    level.volume = total_vol
                self._update_total_bid(voldiff, price)
                return
            if level.price < price and total_vol > 0:
                # insert before here and return
                lnew = Order(price, total_vol, "ask")
                self.bids.insert(i, lnew)
                self._update_total_bid(total_vol, price)
                return

        # still here? -> end of list or empty list.
        if total_vol > 0:
            lnew = Order(price, total_vol, "ask")
            self.bids.append(lnew)
            self._update_total_bid(total_vol, price)
#added this
            self.bid = self.bids[0].price

    def _update_total_ask(self, volume):
        """update total BTC on the ask side"""
        self.total_ask += int2float(volume, "BTC")

    def _update_total_bid(self, volume, price):
        """update total fiat on the bid side"""
        self.total_bid += int2float(volume, "BTC") * int2float(price, self.gox.currency)

    def get_own_volume_at(self, price):
        """returns the sum of the volume of own orders at a given price"""
        volume = 0
        for order in self.owns:
            print "order price is %s, price we are checking is %s" % (order.price,price)
            if order.price == price:
                volume += order.volume
        return volume

    def have_own_oid(self, oid):
        """do we have an own order with this oid in our list already?"""
        for order in self.owns:
            if order.oid == oid:
                return True
        return False

    def reset_own(self):
        """clear all own orders"""
        self.owns = []
        self.signal_changed(self, ())

    def add_own(self, order):
        """add order to the list of own orders. This method is used
        by the Gox object only during initial download of complete
        order list, all subsequent updates will then be done through
        the event methods slot_user_order and slot_trade"""

        def insert_dummy(lst, is_ask):
            """insert an empty (volume=0) dummy order into the bids or asks
            to make the own order immediately appear in the UI, even if we
            don't have the full orderbook yet. The dummy orders will be updated
            later to reflect the true total volume at these prices once we get
            authoritative data from the server"""
            for i in range (len(lst)):
                existing = lst[i]
                if existing.price == order.price:
                    return # no dummy needed, an order at this price exists
                if is_ask:
                    if existing.price > order.price:
                        lst.insert(i, Order(order.price, 0, order.typ))
                        return
                else:
                    if existing.price < order.price:
                        lst.insert(i, Order(order.price, 0, order.typ))
                        return

            # end of list or empty
            lst.append(Order(order.price, 0, order.typ))

        if not self.have_own_oid(order.oid):
            self.owns.append(order)

            if order.typ == "ask":
                insert_dummy(self.asks, True)
            if order.typ == "bid":
                insert_dummy(self.bids, False)

            self.signal_changed(self, ())
