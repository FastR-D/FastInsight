"""Install only the external HTTP fixture in scenario-owned Python children."""
import os
import urllib.request

from http_transport import route_urlopen

if os.environ.get("FASTINSIGHT_FIXTURE_ORIGIN"):
    urllib.request.urlopen = route_urlopen(
        urllib.request.urlopen, os.environ["FASTINSIGHT_FIXTURE_ORIGIN"]
    )
