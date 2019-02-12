
import json
import uuid

from notebook.base.handlers import IPythonHandler
from notebook.base.zmqhandlers import WebSocketMixin
from tornado import gen, web
from tornado.websocket import WebSocketHandler

from .db import DatastoreDB


class WSBaseHandler(WebSocketMixin, WebSocketHandler, IPythonHandler):
    """Base class for websockets reusing jupyter code"""

    def set_default_headers(self):
        """Undo the set_default_headers in IPythonHandler

        which doesn't make sense for websockets
        """
        pass

    def pre_get(self):
        """Run before finishing the GET request

        Extend this method to add logic that should fire before
        the websocket finishes completing.
        """
        # authenticate the request before opening the websocket
        if self.get_current_user() is None:
            self.log.warning("Couldn't authenticate WebSocket connection")
            raise web.HTTPError(403)

    @gen.coroutine
    def get(self, *args, **kwargs):
        # pre_get can be a coroutine in subclasses
        # assign and yield in two step to avoid tornado 3 issues
        res = self.pre_get()
        yield gen.maybe_future(res)
        super(WSBaseHandler, self).get(*args, **kwargs)

    def get_compression_options(self):
        return self.settings.get('websocket_compression_options', None)


def create_storeid_reply(parent_id, store_id):
    return dict(
        msgId=str(uuid.uuid4()),
        msgType='storeid-reply',
        parentId=parent_id,
        content=dict(
            storeId=store_id
        )
    )

def create_transactions_ack(parent_id, transactions):
    return dict(
        msgId=str(uuid.uuid4()),
        msgType='transaction-ack',
        parentId=parent_id,
        content=dict(
            transactionIds=[t['id'] for t in transactions]
        )
    )

def create_history_reply(parent_id, transactions):
    return dict(
        msgId=str(uuid.uuid4()),
        msgType='history-reply',
        parentId=parent_id,
        content=dict(
            history=dict(
                transactions=transactions
            )
        )
    )

def create_fetch_reply(parent_id, transactions):
    return dict(
        msgId=str(uuid.uuid4()),
        msgType='fetch-transaction-reply',
        parentId=parent_id,
        content=dict(
            transactions=transactions
        )
    )


# TODO: Write a shared manager for all datastore handlers
# Keeps one DB per key
# Correctly broadcasts messages to other handlers
class DatastoreHandler(WSBaseHandler):

    store_ids = {} # map of RTC store key -> store id serial
    stores = {} # map of RTC store key -> datastore dbs
    handlers = {} # map of RTC store key -> handlers

    def initialize(self):
        self.log.info("Initializing datastore connection %s", self.request.path)
        self.db = None

    def get_db_file(self):
        # TODO: user setting? Execution dir with pid? For now, use in-memory
        return ':memory:'

    def create_store_id(self):
        self.store_ids[self.store_key] = 1 + self.store_ids[self.store_key]
        return self.store_ids[self.store_key]

    def open(self, store_key=None):
        self.log.info('Datastore open called...')

        if store_key is not None:
            self.store_key = store_key
        else:
            self.log.warning("No store key specified")
            self.store_key = uuid.uuid4()

        if self.store_ids.get(self.store_key, None) is None:
            self.store_ids[self.store_key] = 0

        if self.stores.get(self.store_key, None) is None:
            self.stores[self.store_key] = DatastoreDB(self.get_db_file())
        self.db = self.stores[self.store_key]

        if self.handlers.get(self.store_key, None) is None:
            self.handlers[self.store_key] = []
        self.handlers[self.store_key].append(self)

        super(DatastoreHandler, self).open()
        self.log.info('Opened datastore websocket')

    def on_close(self):
        if self.get_db_file() != ':memory:':
            self.db.close()
        self.handlers[self.store_key].remove(self)
        super(DatastoreHandler, self).on_close()
        self.log.info('Closed datastore websocket')

    def broadcast(self, message):
        for handler in self.handlers[self.store_key]:
            if handler is self:
                continue
            handler.write_message(message)

    def on_message(self, message):
        msg = json.loads(message)
        msg_type = msg.pop('msgType', None)
        msg_id = msg.pop('msgId', None)
        reply = None

        self.log.info('Received datastore message %s: \n%r' % (msg_type, msg))

        if msg_type == 'transaction-broadcast':
            content = msg.pop('content', None)
            if content is None:
                return
            transactions = content.pop('transactions', [])
            self.db.add_transactions(transactions)
            reply = create_transactions_ack(msg_id, transactions)
            self.write_message(json.dumps(reply))
            self.broadcast(message)

        elif msg_type == 'storeid-request':
            reply = create_storeid_reply(msg_id, self.create_store_id())
            self.write_message(json.dumps(reply))

        elif msg_type == 'history-request':
            transactions = tuple(self.db.history())
            reply = create_history_reply(msg_id, transactions)
            self.write_message(json.dumps(reply))

        elif msg_type == 'fetch-transaction-request':
            content = msg.pop('content', None)
            if content is None:
                return
            transactionIds = content.pop('transactionIds', [])
            transactions = tuple(self.db.get_transactions(transactionIds))
            reply = create_fetch_reply(msg_id, transactions)
            self.write_message(json.dumps(reply))

        if reply:
            self.log.info('Sent reply: \n%r' % (reply, ))



# The path for lab build.
# TODO: Is this a reasonable path?
datastore_path = r"/lab/api/datastore/(?P<store_key>\w+)"