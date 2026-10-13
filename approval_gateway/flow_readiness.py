"""Read-only Meta Flow diagnostics. Does not publish, send, or enable a Flow."""
import argparse
import json
import re
import urllib.error
import urllib.request

from .config import load_settings
from .whatsapp import _RejectRedirects


def inspect(settings, flow_id):
    if not re.fullmatch(r'[0-9]{1,64}', flow_id or ''):
        raise ValueError('Supply a numeric Flow ID.')
    version = settings.whatsapp_api_version
    if not re.fullmatch(r'v[0-9]+\.[0-9]+', version):
        raise ValueError('Invalid WhatsApp API version.')
    if not settings.whatsapp_access_token:
        raise ValueError('WhatsApp access token is not configured.')
    request = urllib.request.Request(
        f'https://graph.facebook.com/{version}/{flow_id}?fields=id,status,validation_errors,health_status',
        headers={'Authorization': 'Bearer ' + settings.whatsapp_access_token, 'Accept': 'application/json'})
    with urllib.request.build_opener(_RejectRedirects()).open(request, timeout=15) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Meta response exceeded the diagnostic limit.')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Meta returned an invalid response.')
    return {key: result.get(key) for key in ('id', 'status', 'validation_errors', 'health_status')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flow-id', help='Defaults to WHATSAPP_REJECTION_FLOW_ID')
    args = parser.parse_args()
    try:
        settings = load_settings()
        report = inspect(settings, args.flow_id or settings.whatsapp_rejection_flow_id)
    except urllib.error.HTTPError as exc:
        print(f'Meta lookup failed (HTTP {exc.code}). Check token permissions, Flow ID and owning WABA.')
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print('Lookup failed. Check configuration, Flow ID and network; no credentials were printed.')
        raise SystemExit(1) from None
    print(json.dumps(report, indent=2))
    print('Read-only check. Live rollout still requires a published Flow, no validation errors, '
          'sendable health status and a successful device/webhook test. Missing health data is inconclusive.')


if __name__ == '__main__':
    main()
