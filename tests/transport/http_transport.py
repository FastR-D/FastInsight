"""Route the urllib HTTP boundary to a local protocol fixture, including children."""
import urllib.request


def route_urlopen(original, origin):
    def open_local(request, *args, **kwargs):
        if isinstance(request, str):
            request = urllib.request.Request(request)
        if not request.full_url.startswith(origin + "/"):
            request = urllib.request.Request(
                origin + "/external",
                data=request.data,
                headers={**dict(request.header_items()), "X-Fixture-Url": request.full_url},
                method=request.get_method(),
            )
        return original(request, *args, **kwargs)
    return open_local
