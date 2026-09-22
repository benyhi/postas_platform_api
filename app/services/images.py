import base64
import ipaddress
import mimetypes
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import Settings


MAX_REDIRECTS = 5
ALLOWED_PORTS = {80, 443}


@dataclass(frozen=True)
class FetchedImage:
    url: str
    content_type: str
    size_bytes: int
    data_uri: str


def _validate_public_http_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('file_url debe ser una URL publica http o https')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('file_url no puede incluir credenciales')

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('file_url contiene un puerto invalido') from exc
    effective_port = port or (443 if parsed.scheme == 'https' else 80)
    if effective_port not in ALLOWED_PORTS:
        raise ValueError('file_url utiliza un puerto no permitido')

    hostname = parsed.hostname.rstrip('.').casefold()
    if hostname == 'localhost' or hostname.endswith('.localhost'):
        raise ValueError('file_url debe apuntar a una direccion publica')

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(sockaddr[0])
                for _, _, _, _, sockaddr in socket.getaddrinfo(
                    hostname,
                    effective_port,
                    type=socket.SOCK_STREAM,
                )
            }
        except (socket.gaierror, ValueError) as exc:
            raise ValueError('No se pudo resolver file_url') from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError('file_url debe apuntar a una direccion publica')
    pinned_address = sorted(addresses, key=lambda address: (address.version, str(address)))[0]
    return parsed, pinned_address


def _pinned_url(parsed, address) -> str:
    host = f'[{address}]' if address.version == 6 else str(address)
    if parsed.port is not None:
        host = f'{host}:{parsed.port}'
    return parsed._replace(netloc=host).geturl()


def _download_with_client(
    url: str,
    settings: Settings,
    client: httpx.Client,
) -> tuple[str, bytes]:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        parsed, address = _validate_public_http_url(current_url)
        request_extensions = {}
        if parsed.scheme == 'https':
            request_extensions['sni_hostname'] = parsed.hostname.encode('idna')
        with client.stream(
            'GET',
            _pinned_url(parsed, address),
            headers={'Host': parsed.netloc},
            extensions=request_extensions,
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError('file_url excede el maximo de redirecciones')
                location = response.headers.get('location')
                if not location:
                    raise ValueError('file_url devolvio una redireccion invalida')
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            content_type = _normalize_content_type(
                response.headers.get('content-type'),
                parsed.path,
            )
            if not content_type.startswith('image/'):
                raise ValueError(f'file_url no apunta a una imagen valida: {content_type}')

            content_length = response.headers.get('content-length')
            if content_length:
                try:
                    if int(content_length) > settings.max_image_bytes:
                        raise ValueError('La imagen supera el tamano maximo permitido')
                except ValueError as exc:
                    if str(exc) == 'La imagen supera el tamano maximo permitido':
                        raise

            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > settings.max_image_bytes:
                    raise ValueError('La imagen supera el tamano maximo permitido')
            return content_type, bytes(content)

    raise ValueError('file_url excede el maximo de redirecciones')


def fetch_public_image_as_data_uri(
    url: str,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> FetchedImage:
    timeout = httpx.Timeout(settings.image_download_timeout_seconds)
    if client is None:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as owned_client:
            content_type, content = _download_with_client(url, settings, owned_client)
    else:
        content_type, content = _download_with_client(url, settings, client)

    encoded = base64.b64encode(content).decode('ascii')
    return FetchedImage(
        url=url,
        content_type=content_type,
        size_bytes=len(content),
        data_uri=f'data:{content_type};base64,{encoded}',
    )


def _normalize_content_type(header_value: str | None, path: str) -> str:
    if header_value:
        return header_value.split(';', 1)[0].strip().lower()

    guessed_type, _ = mimetypes.guess_type(path)
    if guessed_type:
        return guessed_type.lower()
    return 'application/octet-stream'
