"""Demo tooling only: lets the control plane reach the mock identity provider.

Put on PYTHONPATH for the control-plane process by scripts/demo-console/run.sh. The demo
identity provider's issuer is https://<DEMO_IDP_HOST>/ (the Auth0 SDK accepts no port or
localhost), so that name resolves to a reserved loopback address and connections to it on
port 443 go to DEMO_IDP_ADDR instead. TLS is still verified against the name, with
SSL_CERT_FILE pointing at the demo CA. No other host or port is affected.
"""

import os
import socket

_host = os.environ.get("DEMO_IDP_HOST", "").lower()
_address, _, _port = os.environ.get("DEMO_IDP_ADDR", "").partition(":")
_SENTINEL = "127.77.0.1"

if _host and _address and _port:
    _getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower() == _host:
            host = _SENTINEL
        return _getaddrinfo(host, port, *args, **kwargs)

    def _target(address):
        if isinstance(address, tuple) and len(address) >= 2 and address[0] == _SENTINEL \
                and int(address[1]) == 443:
            return (_address, int(_port))
        return address

    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex

    def connect(self, address):
        return _connect(self, _target(address))

    def connect_ex(self, address):
        return _connect_ex(self, _target(address))

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
