import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def local_connections_only(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    resolve = socket.getaddrinfo

    def check_host(value):
        if value in ("localhost", None):
            return
        try:
            if ipaddress.ip_address(value).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError("external network is forbidden in local tests")

    def guarded_connect(instance, address):
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect(instance, address)

    def guarded_connect_ex(instance, address):
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect_ex(instance, address)

    def guarded_resolve(host, *args, **kwargs):
        check_host(host)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
