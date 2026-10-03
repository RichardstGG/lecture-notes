"""Local route suggestions for the sharing UI; never probes an external host."""
import asyncio
import ipaddress
import json
import subprocess


def usable_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError):
        return False
    return not (address.is_unspecified or address.is_loopback
                or address.is_multicast or address.is_reserved)


def _ip_json(*args):
    result = subprocess.run(['ip', '-j', '-4', *args], capture_output=True,
                            text=True, encoding='utf-8', timeout=2, check=True)
    if len(result.stdout) > 65536:
        raise ValueError('route output too large')
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise ValueError('expected ip JSON array')
    return data


def _default_route():
    routes = _ip_json('route', 'show', 'default')
    routes = sorted((r for r in routes if isinstance(r, dict) and r.get('dev')
                     and r.get('type', 'unicast') == 'unicast'),
                    key=lambda r: int(r.get('metric', 0)))
    for route in routes:
        interface = route['dev']
        if not isinstance(interface, str) or not interface or interface.startswith('-'):
            continue
        links = _ip_json('addr', 'show', 'dev', interface)
        addresses = [a['local'] for link in links for a in link.get('addr_info', [])
                     if a.get('family') == 'inet' and a.get('scope') == 'global'
                     and usable_ipv4(a.get('local'))]
        if addresses:
            preferred = route.get('prefsrc')
            return {'api_version': 1, 'interface': interface,
                    'advertise_host': preferred if preferred in addresses else addresses[0]}
    raise ValueError('no default route address')


async def sharing_network():
    try:
        return await asyncio.to_thread(_default_route)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError):
        return {'api_version': 1, 'interface': None, 'advertise_host': None,
                'error': '無法偵測預設路由 IPv4，請手動輸入本機可連線的 IP。'}
