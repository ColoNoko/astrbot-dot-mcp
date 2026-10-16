import json
from pathlib import Path
def assert_legacy_schema(test, schema):
    expected=json.loads((Path(__file__).parent/'schema_v0415_contract.json').read_text())
    for key, fields in expected.items():
        test.assertIn(key,schema)
        for field,value in fields.items():
            if key == "callback_backend" and field == "options":
                test.assertTrue(schema[key].get("invisible")); continue
            test.assertEqual(schema[key].get(field),value,(key,field))
