import json
from pathlib import Path
import unittest


class RecordsTest(unittest.TestCase):
    def test_known_input_contract(self):
        source = Path(__file__).resolve().parents[1] / "data" / "records.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        requests = {item['request']: item for item in payload['requests']}
        assert all(item['request'] in requests for table in ('events', 'windows', 'pauses', 'rebook_requests', 'amendments') for item in payload[table])
        assert {'发出', '接手', '转抄', '退回'} <= {item['event'] for item in payload['events']}
        assert {item['join'] for item in payload['requests']} == {'全部', '任一'}
        assert any(item['notified'] == '已通知' for item in payload['windows'])
