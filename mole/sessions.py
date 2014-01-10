# -*- coding: utf-8 -*-
"""A fast, lightweight, and secure session WSGI middleware for use with GAE."""
from Cookie import CookieError, SimpleCookie
from base64 import b64decode, b64encode
import datetime
import hashlib
import hmac
import logging
import pickle
import os
import threading
import time

#import memcache
#from sqlalchemy.ext.declarative import declarative_base, Column
#from sqlalchemy.types import BLOB,VARCHAR,INT,DATETIME
#from sqlalchemy.orm import sessionmaker
#from sqlalchemy.exc import SQLAlchemyError

#from google.appengine.ext import db

# Configurable cookie options
COOKIE_NAME_PREFIX = "DgU"  # identifies a cookie as being one used by bottle-sessions (so you can set cookies too)
COOKIE_PATH = "/"
DEFAULT_COOKIE_ONLY_THRESH = 4096  # most *nix paging size is 4096. There is no limit in header size in HTTP spec, it is nontheless limited by system's page size

DEFAULT_LIFETIME = datetime.timedelta(seconds=60*30)#datetime.timedelta(days=7)

#SessionModel Recycle tag
SM_RECYCLE_TAG  = "_SESSIONMODEL_RECYCLE_TAG_"

# constants
SID_LEN = 43  # timestamp (10 chars) + underscore + md5 (32 hex chars)
SIG_LEN = 44  # base 64 encoded HMAC-SHA256
MAX_COOKIE_LEN = 4096
EXPIRE_COOKIE_FMT = ' %s=; expires=Wed, 01-Jan-1970 00:00:00 GMT; Path=' + COOKIE_PATH
COOKIE_FMT = ' ' + COOKIE_NAME_PREFIX + '%02d="%s"; %sPath=' + COOKIE_PATH + '; HttpOnly'
COOKIE_FMT_SECURE = COOKIE_FMT + '; Secure'
COOKIE_DATE_FMT = '%a, %d-%b-%Y %H:%M:%S GMT'
COOKIE_OVERHEAD = len(COOKIE_FMT % (0, '', '')) + len('expires=Xxx, xx XXX XXXX XX:XX:XX GMT; ') + 150  # 150=safety margin (e.g., in case browser uses 4000 instead of 4096)
MAX_DATA_PER_COOKIE = MAX_COOKIE_LEN - COOKIE_OVERHEAD

_tls = threading.local()
#SADeclarativeModel = declarative_base()

def get_current_session():
    """Returns the session associated with the current request."""
    return _tls.current_session

def set_current_session(session):
    """Sets the session associated with the current request."""
    _tls.current_session = session

def is_bottle_sessions_key(k):
    return k.startswith(COOKIE_NAME_PREFIX)

#class SessionModel(SADeclarativeModel):
#    """Contains session data.  sid is the session ID and pdump contains a
#    pickled dictionary which maps session variables to their values."""
#    __tablename__ = 'tbl_bottle_session'
#
#    id = Column(INT(unsigned=True), nullable=False, primary_key=True)
#    ts = Column(INT,nullable=False,index=True)
#    sid = Column(VARCHAR(255),nullable=False,index=True)
#    pdump = Column(BLOB)
#
#    def __init__(self,timestamp,sid,pdump):
#        self.ts = timestamp
#        self.sid = sid
#        self.pdump = pdump
#
#    def __repr__(self):
#        return '<SessionModel id{0} timestamp:{1} sid:{2}>'.format(self.id,self.ts,self.sid)
#from peewee import *
#database = MySQLDatabase('zkeco_db', **{'passwd': 'root', 'user': 'root'})
Model = object
database = None

class SessionModel(Model):
#    id = IntegerField(unique=True)
#    ts = IntegerField()
#    sid = CharField()
#    pdump =BlobField()

    def __unicode__(self):
        return self.sid
    
    class Meta:
        database = database

class Session(object):
    """Manages loading, reading/writing key-value pairs, and saving of a session.

    ``sid`` - if set, then the session for that sid (if any) is loaded. Otherwise,
    sid will be loaded from the HTTP_COOKIE (if any).
    """
    DIRTY_BUT_DONT_PERSIST_TO_DB = 1

    def __init__(self, sid=None, environ=None, lifetime=DEFAULT_LIFETIME, no_datastore=False,
                 cookie_only_threshold=DEFAULT_COOKIE_ONLY_THRESH, cookie_key=None):
        self._accessed = False
        self.sid = None
        self.cookie_keys = []
        self.cookie_data = None
        self.data = {}
        self.dirty = False  # has the session been changed?

        #python os.environ doesn't work in python 2.7.3 
        #Get the reference instantiation time
        self.environ = environ if environ is not None else os.environ.copy()

        #SQLAlchemy Session
        self.sa_session_class = None#sa_session_class  @add

        #python-memcached client
        self.mc_client = None#mc_client    @add

        self.lifetime = lifetime
        self.no_datastore = no_datastore
        self.cookie_only_thresh = cookie_only_threshold
        self.base_key = cookie_key

        if sid:
            self.__set_sid(sid, False)
            self.data = None
        else:
            self.__read_cookie()

    @staticmethod
    def __compute_hmac(base_key, sid, text):
        """Computes the signature for text given base_key and sid."""
        key = base_key + sid
        return b64encode(hmac.new(key, text, hashlib.sha256).digest())

    def __read_cookie(self):
        """Reads the HTTP Cookie and loads the sid and data from it (if any)."""
        try:
            #check if we could get any piece of cookie at all.
            #Cookie string, sometimes, is complete nil
            if self.environ.get('HTTP_COOKIE') is None:
                return #no cookies string what-so-ever

            # check the cookie to see if a session has been started
            #cookie = SimpleCookie(os.environ['HTTP_COOKIE'])
            cookie = SimpleCookie(self.environ.get('HTTP_COOKIE'))
            self.cookie_keys = filter(is_bottle_sessions_key, cookie.keys())
            if not self.cookie_keys:
                return  # no session yet

            self.cookie_keys.sort()
            data = ''.join(cookie[k].value for k in self.cookie_keys)
            i = SIG_LEN + SID_LEN
            sig, sid, b64pdump = data[:SIG_LEN], data[SIG_LEN:i], data[i:]
            pdump = b64decode(b64pdump)
            actual_sig = Session.__compute_hmac(self.base_key, sid, pdump)
            if sig == actual_sig:
                self.__set_sid(sid, False)
                # check for expiration and terminate the session if it has expired
                if self.get_expiration() != 0 and time.time() > self.get_expiration():
                    return self.terminate()

                if pdump:
                    self.data = self.__decode_data(pdump)
                else:
                    self.data = None  # data is in memcache/db: load it on-demand
            else:
                logging.warn('cookie with invalid sig received from %s: %s' % (os.environ.get('REMOTE_ADDR'), b64pdump))
        except (CookieError, KeyError, IndexError, TypeError):
            # there is no cookie (i.e., no session) or the cookie is invalid
            self.terminate(False)

    def make_cookie_headers(self):
        """Returns a list of cookie headers to send (if any)."""
        # expire all cookies if the session has ended
        if not self.sid:
            return [EXPIRE_COOKIE_FMT % k for k in self.cookie_keys]

        if self.cookie_data is None:
            return []  # no cookie headers need to be sent

        # build the cookie header(s): includes sig, sid, and cookie_data
        if self.is_ssl_only():
            m = MAX_DATA_PER_COOKIE - 8
            fmt = COOKIE_FMT_SECURE
        else:
            m = MAX_DATA_PER_COOKIE
            fmt = COOKIE_FMT
        sig = Session.__compute_hmac(self.base_key, self.sid, self.cookie_data)
        cv = sig + self.sid + b64encode(self.cookie_data)
        num_cookies = 1 + (len(cv) - 1) / m
        if self.get_expiration() > 0:
            ed = "expires=%s; " % datetime.datetime.fromtimestamp(self.get_expiration()).strftime(COOKIE_DATE_FMT)
        else:
            ed = ''
        cookies = [fmt % (i, cv[i * m:i * m + m], ed) for i in xrange(num_cookies)]

        # expire old cookies which aren't needed anymore
        old_cookies = xrange(num_cookies, len(self.cookie_keys))
        key = COOKIE_NAME_PREFIX + '%02d'
        cookies_to_ax = [EXPIRE_COOKIE_FMT % (key % i) for i in old_cookies]
        return cookies + cookies_to_ax

    def is_active(self):
        """Returns True if this session is active (i.e., it has been assigned a
        session ID and will be or has been persisted)."""
        return self.sid is not None

    def is_ssl_only(self):
        """Returns True if cookies set by this session will include the "Secure"
        attribute so that the client will only send them over a secure channel
        like SSL)."""
        return self.sid is not None and self.sid[-33] == 'S'

    def is_accessed(self):
        """Returns True if any value of this session has been accessed."""
        return self._accessed

    def ensure_data_loaded(self):
        """Fetch the session data if it hasn't been retrieved it yet."""
        self._accessed = True
        if self.data is None and self.sid:
            self.__retrieve_data()

    def get_expiration(self):
        """Returns the timestamp at which this session will expire."""
        try:
            return int(self.sid[:-33])
        except:
            return 0

    def __make_sid(self, expire_ts=None, ssl_only=False):
        """Returns a new session ID."""
        # make a random ID (random.randrange() is 10x faster but less secure?)
        if expire_ts is None:
            expire_dt = datetime.datetime.now() + self.lifetime
            expire_ts = int(time.mktime((expire_dt).timetuple()))
        else:
            expire_ts = int(expire_ts)
        if ssl_only:
            sep = 'S'
        else:
            sep = '_'
        return ('%010d' % expire_ts) + sep + hashlib.md5(os.urandom(16)).hexdigest()

    @staticmethod
    def __encode_data(d):
        """Returns a "pickled+" encoding of d.  d values of type db.Model are
        protobuf encoded before pickling to minimize CPU usage & data size."""
        # separate protobufs so we'll know how to decode (they are just strings)
        eP = {}  # for models encoded as protobufs
        eO = {}  # for everything else
        for k, v in d.iteritems():
            if isinstance(v, SessionModel):
                #eP[k] = db.model_to_protobuf(v)
                eP[k] = v
            else:
                eO[k] = v
        return pickle.dumps((eP, eO), 2)

    @staticmethod
    def __decode_data(pdump):
        """Returns a data dictionary after decoding it from "pickled+" form."""
        try:
            eP, eO = pickle.loads(pdump)
            for k, v in eP.iteritems():
                #eO[k] = db.model_from_protobuf(v)
                eO[k] = v
        except Exception, e:
            logging.warn("failed to decode session data: %s" % e)
            eO = {}
        return eO

    def regenerate_id(self, expiration_ts=None):
        """Assigns the session a new session ID (data carries over).  This
        should be called whenever a user authenticates to prevent session
        fixation attacks.

        ``expiration_ts`` - The UNIX timestamp the session will expire at. If
        omitted, the session expiration time will not be changed.
        """
        if self.sid or expiration_ts is not None:
            self.ensure_data_loaded()  # ensure we have the data before we delete it
            if expiration_ts is None:
                expiration_ts = self.get_expiration()
            self.__set_sid(self.__make_sid(expiration_ts, self.is_ssl_only()))
            self.dirty = True  # ensure the data is written to the new session

    def start(self, expiration_ts=None, ssl_only=False):
        """Starts a new session.  expiration specifies when it will expire.  If
        expiration is not specified, then self.lifetime will used to
        determine the expiration date.

        Normally this method does not need to be called directly - a session is
        automatically started when the first value is added to the session.

        ``expiration_ts`` - The UNIX timestamp the session will expire at. If
        omitted, the session will expire after the default ``lifetime`` has past
        (as specified in ``SessionMiddleware``).

        ``ssl_only`` - Whether to specify the "Secure" attribute on the cookie
        so that the client will ONLY transfer the cookie over a secure channel.
        """
        self.dirty = True
        self.data = {}
        self.__set_sid(self.__make_sid(expiration_ts, ssl_only), True)

    def terminate(self, clear_data=True):
        """Deletes the session and its data, and expires the user's cookie."""
        if clear_data:
            self.__clear_data()
        self.sid = None
        self.data = {}
        self.dirty = False
        if self.cookie_keys:
            self.cookie_data = ''  # trigger the cookies to expire
        else:
            self.cookie_data = None

    def __set_sid(self, sid, make_cookie=True):
        """Sets the session ID, deleting the old session if one existed.  The
        session's data will remain intact (only the session ID changes)."""
        if self.sid:
            self.__clear_data()
        self.sid = sid
        #With SQLAlchemy, sid is unique enough for identification. so we do not need db_key
        #self.db_key = db.Key.from_path(SessionModel.kind(), sid, namespace='')

        # set the cookie if requested
        if make_cookie:
            self.cookie_data = ''  # trigger the cookie to be sent

    def __clear_data(self):
        """Deletes this session from memcache and the datastore."""
        if self.sid:
            try:
                self.mc_client.delete(self.sid) # not really needed; it'll go away on its own
                sa_session_instance = self.sa_session_class()
                """Recycle SessionModel due to limited index issue. set sid to recycle tag,
                and erase pdump"""
                session_model_instance = \
                    sa_session_instance\
                        .query(SessionModel)\
                        .filter(SessionModel.sid == self.sid)\
                        .first()

                if session_model_instance:
                    session_model_instance.ts = 0
                    session_model_instance.sid = SM_RECYCLE_TAG
                    session_model_instance.pdump = None
                    sa_session_instance.commit()

            except:
                pass  # either it wasn't in the db (maybe cookie/memcache-only) or db is down => cron will expire it

    def __retrieve_data(self):
        """Sets the data associated with this session after retrieving it from
        memcache or the datastore.  Assumes self.sid is set.  Checks for session
        expiration after getting the data."""
        pdump = self.mc_client.get(self.sid)
        if pdump is None:
            # memcache lost it, go to the datastore
            if self.no_datastore:
                logging.info("can't find session data in memcache for sid=%s (using memcache only sessions)" % self.sid)
                self.terminate(False)  # we lost it; just kill the session
                return

            try:
                sa_session_instance = self.sa_session_class()

                session_model_instance = \
                    sa_session_instance\
                        .query(SessionModel)\
                        .filter(SessionModel.sid == self.sid)\
                        .first()

                if session_model_instance:
                    pdump = session_model_instance.pdump
                    session_model_instance.ts = int(time.time())
                    sa_session_instance.commit()
                else:
                    logging.error("can't find session data in the datastore for sid=%s" % self.sid)
                    self.terminate(False)  # we lost it; just kill the session
                    return

            except Exception, e:
                logging.warning("unable to retrieve session from datastore for sid=%s (%s)" % (self.sid, e))

        self.data = self.__decode_data(pdump)

    def save(self, persist_even_if_using_cookie=False):
        """Saves the data associated with this session IF any changes have been
        made (specifically, if any mutator methods like __setitem__ or the like
        is called).

        If the data is small enough it will be sent back to the user in a cookie
        instead of using memcache and the datastore.  If `persist_even_if_using_cookie`
        evaluates to True, memcache and the datastore will also be used.  If the
        no_datastore option is set, then the datastore will never be used.

        Normally this method does not need to be called directly - a session is
        automatically saved at the end of the request if any changes were made.
        """
        if not self.sid:
            return  # no session is active
        if not self.dirty:
            return  # nothing has changed

        dirty = self.dirty
        self.dirty = False  # saving, so it won't be dirty anymore

        # do the pickling ourselves b/c we need it for the datastore anyway
        pdump = self.__encode_data(self.data)

        # persist via cookies if it is reasonably small
        if len(pdump) * 4 / 3 <= self.cookie_only_thresh:  # 4/3 b/c base64 is ~33% bigger
            self.cookie_data = pdump
            if not persist_even_if_using_cookie:
                return
        elif self.cookie_keys:
            # latest data will only be in the backend, so expire data cookies we set
            self.cookie_data = ''

        self.mc_client.set(key=self.sid, val=pdump, time=self.get_expiration()) # may fail if memcache is down

        # persist the session to the datastore
        if dirty is Session.DIRTY_BUT_DONT_PERSIST_TO_DB or self.no_datastore:
            return

        try:
            sa_session_instance = self.sa_session_class()

            """Try finding a SessionModel instance with the identical sid"""
            session_model_instance = \
                sa_session_instance\
                    .query(SessionModel)\
                    .filter(SessionModel.sid == self.sid)\
                    .first()

            if session_model_instance is not None:
                session_model_instance.ts = int(time.time())
                session_model_instance.pdump = pdump
                sa_session_instance.commit()
                return

            """Try pulling a SessionModel instance to recycle pool"""
            session_model_instance = \
                sa_session_instance\
                    .query(SessionModel)\
                    .filter(SessionModel.sid == SM_RECYCLE_TAG)\
                    .first()

            if session_model_instance:
                session_model_instance.ts = int(time.time())
                session_model_instance.sid = self.sid
                session_model_instance.pdump = pdump
            else:
                #if there is none, create one
                sa_session_instance.add(SessionModel(timestamp=int(time.time()),sid=self.sid,pdump=pdump))
            
            sa_session_instance.commit()

        except Exception, e:
            logging.warning("unable to persist session to datastore for sid=%s (%s)" % (self.sid, e))

    # Users may interact with the session through a dictionary-like interface.
    def clear(self):
        """Removes all data from the session (but does not terminate it)."""
        if self.sid:
            self.data = {}
            self.dirty = True

    def get(self, key, default=None):
        """Retrieves a value from the session."""
        self.ensure_data_loaded()
        return self.data.get(key, default)

    def has_key(self, key):
        """Returns True if key is set."""
        self.ensure_data_loaded()
        return key in self.data

    def pop(self, key, default=None):
        """Removes key and returns its value, or default if key is not present."""
        self.ensure_data_loaded()
        self.dirty = True
        return self.data.pop(key, default)

    def pop_quick(self, key, default=None):
        """Removes key and returns its value, or default if key is not present.
        The change will only be persisted to memcache until another change
        necessitates a write to the datastore."""
        self.ensure_data_loaded()
        if self.dirty is False:
            self.dirty = Session.DIRTY_BUT_DONT_PERSIST_TO_DB
        return self.data.pop(key, default)

    def set_quick(self, key, value):
        """Set a value named key on this session.  The change will only be
        persisted to memcache until another change necessitates a write to the
        datastore.  This will start a session if one is not already active."""
        dirty = self.dirty
        self[key] = value
        if dirty is False or dirty is Session.DIRTY_BUT_DONT_PERSIST_TO_DB:
            self.dirty = Session.DIRTY_BUT_DONT_PERSIST_TO_DB

    def __getitem__(self, key):
        """Returns the value associated with key on this session."""
        self.ensure_data_loaded()
        return self.data.__getitem__(key)

    def __setitem__(self, key, value):
        """Set a value named key on this session.  This will start a session if
        one is not already active."""
        self.ensure_data_loaded()
        if not self.sid:
            self.start()
        self.data.__setitem__(key, value)
        self.dirty = True

    def __delitem__(self, key):
        """Deletes the value associated with key on this session."""
        self.ensure_data_loaded()
        self.data.__delitem__(key)
        self.dirty = True

    def __iter__(self):
        """Returns an iterator over the keys (names) of the stored values."""
        self.ensure_data_loaded()
        return self.data.iterkeys()

    def __contains__(self, key):
        """Returns True if key is present on this session."""
        self.ensure_data_loaded()
        return self.data.__contains__(key)

    def __str__(self):
        """Returns a string representation of the session."""
        if self.sid:
            self.ensure_data_loaded()
            return "SID=%s %s" % (self.sid, self.data)
        else:
            return "uninitialized session"


class SessionMiddleware(object):
    """WSGI middleware that adds session support.

    ``cookie_key`` - A key used to secure cookies so users cannot modify their
    content.  Keys should be at least 32 bytes (RFC2104).  Tip: generate your
    key using ``os.urandom(64)`` but do this OFFLINE and copy/paste the output
    into a string which you pass in as ``cookie_key``.  If you use ``os.urandom()``
    to dynamically generate your key at runtime then any existing sessions will
    become junk every time your app starts up!

    ``lifetime`` - ``datetime.timedelta`` that specifies how long a session may last.  Defaults to 7 days.

    ``no_datastore`` - By default all writes also go to the datastore in case
    memcache is lost.  Set to True to never use the datastore. This improves
    write performance but sessions may be occassionally lost.

    ``cookie_only_threshold`` - A size in bytes.  If session data is less than this
    threshold, then session data is kept only in a secure cookie.  This avoids
    memcache/datastore latency which is critical for small sessions.  Larger
    sessions are kept in memcache+datastore instead.  Defaults to 10KB.
    """
    def __init__(self, app, cookie_key, lifetime=DEFAULT_LIFETIME, no_datastore=False, cookie_only_threshold=DEFAULT_COOKIE_ONLY_THRESH):
        self.app = app
#        self.sa_session_class = sa_session_class
#        self.mc_client = mc_client
        self.lifetime = lifetime
        self.no_datastore = no_datastore
        self.cookie_only_thresh = cookie_only_threshold
        self.cookie_key = cookie_key
        if not self.cookie_key:
            raise ValueError("cookie_key MUST be specified")
        if len(self.cookie_key) < 32:
            raise ValueError("RFC2104 recommends you use at least a 32 character key.  Try os.urandom(64) to make a key.")

    def __call__(self, environ, start_response):
        # initialize a session for the current user
        _tls.current_session = Session(environ=environ, lifetime=self.lifetime, no_datastore=self.no_datastore, cookie_only_threshold=self.cookie_only_thresh, cookie_key=self.cookie_key)
        # create a hook for us to insert a cookie into the response headers
        def bottle_session_start_response(status, headers, exc_info=None):
            _tls.current_session.save()  # store the session if it was changed
            for ch in _tls.current_session.make_cookie_headers():
                headers.append(('Set-Cookie', ch))
            return start_response(status, headers, exc_info)

        # let the app do its thing
        return self.app(environ, bottle_session_start_response)



def delete_expired_sessions(sa_session_class,session_lifetime=DEFAULT_LIFETIME):
    """Deletes expired sessions from the datastore.
    Returns True if all recycled sessions are less than 500
    """
    def timedelta_to_second(td):
        return (td.seconds + td.days * 86400)

    sa_session_instance = sa_session_class()
    expire_thresh = int(time.time()) - timedelta_to_second(session_lifetime)

    print "now {} | expire_thresh {}".format(int(time.time()),expire_thresh)

    session_models =\
        sa_session_instance\
            .query(SessionModel)\
            .filter(SessionModel.ts < expire_thresh)\
            .filter(SessionModel.sid != SM_RECYCLE_TAG)\
            .all()

    for sm in session_models:
        sm.ts = 0
        sm.sid = SM_RECYCLE_TAG
        sm.pdump = None
    logging.info('bottle-sessions: recycled %d expired sessions from the datastore' % len(session_models))
    sa_session_instance.commit()
    return len(session_models) < 500

def authenticator(login_url = '/login'):
    '''Create an authenticator decorator.
    
    :param login_url: The URL to redirect to if a login is required.
            (default: ``'/auth/login'``).
    '''
    from mole import redirect
    def valid_user(login_url = login_url):
        def decorator(handler, *a, **ka):
            import functools
            @functools.wraps(handler)
            def check_auth(*a, **ka):
                try:
                    session = get_current_session()
                    username = session["username"]
                except (KeyError, TypeError, AttributeError):
                    redirect(login_url)
                return handler(*a, **ka)
            return check_auth
        return decorator
    return(valid_user)

valid_user = authenticator()