from twisted.web.resource import Resource
from twisted.python.failure import Failure
from twisted.python import log
from twisted.web.server import NOT_DONE_YET
from twisted.internet import defer
from logging import DEBUG
from ujson import decode as json_decode, encode as json_encode

import time


class BaseService(Resource):

    def prepare(self, request):
        request.content.seek(0, 0)
        content = request.content.read()
        log.msg("content size = %d" % len(content), level=DEBUG)
        if content:
            return defer.succeed(json_decode(content))
        else:
            return defer.succeed(None)

    def render(self, *args, **kwargs):
        self.startTime = time.time()
        return Resource.render(self, *args, **kwargs)

    def doCancel(self, err, call):
        log.msg("Cancelling current request.", level=DEBUG)
        call.cancel()

    def doResponse(self, value, request):
        request.setHeader("Content-Type", "application/json; charset=UTF-8")
        if isinstance(value, Failure):
            reply = dict(error=str(value.value),
                         traceback=value.getTraceback())
            request.setResponseCode(500)
            request.write(json_encode(reply))

        else:
            request.setResponseCode(200)
            request.write(json_encode(value))

        log.msg("respone time: %.3fms" % (
            (time.time() - self.startTime) * 1000), level=DEBUG)

        request.finish()


class MessageService(BaseService):

    isLeaf = True

    def __init__(self, clients, *args, **kwargs):
        self.clients = clients
        Resource.__init__(self, *args, **kwargs)

    def sendMessage(self, messages):
        if not isinstance(messages, list):
            raise TypeError("input JSON must be a list")

        for message in messages:
            if not isinstance(messages, list):
                raise TypeError("item must be a dict")
            message = dict(map(lambda v: (v[0].encode("UTF-8"), v[1]),
                               message.items()))
            if message["servername"] not in self.clients:
                continue

            self.clients[message["servername"]].protocol.mq_append(message)
            self.clients[message["servername"]].protocol.schedule()

    def render_POST(self, request):
        d = self.prepare(request)
        request.notifyFinish().addErrback(self.doCancel, d)
        d.addCallback(self.sendMessage)
        d.addBoth(self.doResponse, request)
        return NOT_DONE_YET
