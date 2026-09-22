import socket
from types import SimpleNamespace

import httpx
import pytest

from app.services.images import fetch_public_image_as_data_uri


SETTINGS = SimpleNamespace(
    image_download_timeout_seconds=1.0,
    max_image_bytes=1024,
)
PUBLIC_IP = '93.184.216.34'


def _public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, 443))]


def test_rejects_private_ip_without_request():
    with pytest.raises(ValueError, match='direccion publica'):
        fetch_public_image_as_data_uri('http://127.0.0.1/image.png', SETTINGS)


def test_revalidates_redirect_and_rejects_private_target(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _public_dns)

    def handler(request):
        return httpx.Response(
            302,
            headers={'location': 'http://127.0.0.1/secret'},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='direccion publica'):
            fetch_public_image_as_data_uri(
                'https://images.example/photo.png',
                SETTINGS,
                client=client,
            )


def test_pins_validated_address_to_prevent_dns_rebinding(monkeypatch):
    resolutions = 0

    def rebinding_dns(*args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        address = PUBLIC_IP if resolutions == 1 else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]

    monkeypatch.setattr(socket, 'getaddrinfo', rebinding_dns)

    def handler(request):
        assert request.url.host == PUBLIC_IP
        assert request.headers['host'] == 'images.example'
        return httpx.Response(
            200,
            headers={'content-type': 'image/png'},
            content=b'png',
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_public_image_as_data_uri(
            'https://images.example/photo.png',
            SETTINGS,
            client=client,
        )

    assert resolutions == 1
    assert result.size_bytes == 3
