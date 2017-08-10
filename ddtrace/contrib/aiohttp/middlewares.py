import asyncio

from ..asyncio import context_provider
from ...ext import AppTypes, http
from ...compat import stringify


CONFIG_KEY = 'datadog_trace'
REQUEST_CONTEXT_KEY = 'datadog_context'
REQUEST_SPAN_KEY = '__datadog_request_span'

PARENT_TRACE_HEADER_ID = 'x-datadog-trace-id'
PARENT_SPAN_HEADER_ID = 'x-datadog-parent-id'
IS_SAMPLED_HEADER_ID = 'x-datadog-is-sampled'


@asyncio.coroutine
def trace_middleware(app, handler):
    """
    ``aiohttp`` middleware that traces the handler execution.
    Because handlers are run in different tasks for each request, we attach the Context
    instance both to the Task and to the Request objects. In this way:
        * the Task is used by the internal automatic instrumentation
        * the ``Context`` attached to the request can be freely used in the application code
    """
    @asyncio.coroutine
    def attach_context(request):
        # application configs
        tracer = app[CONFIG_KEY]['tracer']
        service = app[CONFIG_KEY]['service']
        distributed_tracing = app[CONFIG_KEY]['distributed_tracing_enabled']

        # trace the handler
        request_span = tracer.trace(
            'aiohttp.request',
            service=service,
            span_type=http.TYPE,
        )

        if distributed_tracing:
            # set parent trace/span IDs if present:
            # http://pypi.datadoghq.com/trace/docs/#distributed-tracing
            parent_trace_id = request.headers.get(PARENT_TRACE_HEADER_ID)
            if parent_trace_id is not None:
                request_span.trace_id = int(parent_trace_id)
                request_span.distributed.trace_id = int(parent_trace_id) # setting it too to avoid confusion
                request_span.tracer().sampler.sample(request_span) # Sampling depends on trace_id.

            parent_span_id = request.headers.get(PARENT_SPAN_HEADER_ID)
            if parent_span_id is not None:
                request_span.parent_id = int(parent_span_id)

            is_sampled = request.headers.get(IS_SAMPLED_HEADER_ID)
            if is_sampled is not None:
                request_span.distributed.sampled = bool(is_sampled)

        # attach the context and the root span to the request; the Context
        # may be freely used by the application code
        request[REQUEST_CONTEXT_KEY] = request_span.context
        request[REQUEST_SPAN_KEY] = request_span
        try:
            response = yield from handler(request)  # noqa: E999
            return response
        except Exception:
            request_span.set_traceback()
            raise
    return attach_context


@asyncio.coroutine
def on_prepare(request, response):
    """
    The on_prepare signal is used to close the request span that is created during
    the trace middleware execution.
    """
    # safe-guard: discard if we don't have a request span
    request_span = request.get(REQUEST_SPAN_KEY, None)
    if not request_span:
        return

    # default resource name
    resource = stringify(response.status)

    if request.match_info.route.resource:
        # collect the resource name based on http resource type
        res_info = request.match_info.route.resource.get_info()

        if res_info.get('path'):
            resource = res_info.get('path')
        elif res_info.get('formatter'):
            resource = res_info.get('formatter')
        elif res_info.get('prefix'):
            resource = res_info.get('prefix')

    request_span.resource = resource
    request_span.set_tag('http.method', request.method)
    request_span.set_tag('http.status_code', response.status)
    request_span.set_tag('http.url', request.path)
    request_span.finish()


def trace_app(app, tracer, service='aiohttp-web'):
    """
    Tracing function that patches the ``aiohttp`` application so that it will be
    traced using the given ``tracer``.

    :param app: aiohttp application to trace
    :param tracer: tracer instance to use
    :param service: service name of tracer
    """

    # safe-guard: don't trace an application twice
    if getattr(app, '__datadog_trace', False):
        return
    setattr(app, '__datadog_trace', True)

    # configure datadog settings
    app[CONFIG_KEY] = {
        'tracer': tracer,
        'service': service,
        'distributed_tracing_enabled': False,
    }

    # the tracer must work with asynchronous Context propagation
    tracer.configure(context_provider=context_provider)

    # configure the current service
    tracer.set_service_info(
        service=service,
        app='aiohttp',
        app_type=AppTypes.web,
    )

    # add the async tracer middleware as a first middleware
    # and be sure that the on_prepare signal is the last one
    app.middlewares.insert(0, trace_middleware)
    app.on_response_prepare.append(on_prepare)
