from ..compatibility.framing import create_framer
from ..models import ConnectionConfig, Transport
from .endpoint import Endpoint, format_peer, parse_peer
from .tcp_client import TcpClient
from .tcp_server import TcpServer
from .udp import UdpClient, UdpServer


def create_endpoint(config: ConnectionConfig) -> Endpoint:
    """Build the endpoint described by a connection configuration."""
    create_framer(config.framing)  # validate the framing options up front
    if config.transport is Transport.TCP_CLIENT:
        return TcpClient(
            config.host, config.port, create_framer(config.framing), config.timeout_ms,
            config.auto_reconnect, config.reconnect_interval_ms,
        )
    if config.transport is Transport.TCP_SERVER:
        return TcpServer(
            config.local_port, config.host, lambda: create_framer(config.framing)
        )
    if config.transport is Transport.UDP_CLIENT:
        return UdpClient(config.host, config.port, config.local_port)
    if config.transport is Transport.UDP_SERVER:
        return UdpServer(config.local_port, config.host)
    raise ValueError(f"unsupported transport {config.transport!r}")


__all__ = [
    "Endpoint", "TcpClient", "TcpServer", "UdpClient", "UdpServer",
    "create_endpoint", "format_peer", "parse_peer",
]
