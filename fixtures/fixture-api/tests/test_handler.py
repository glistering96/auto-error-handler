import unittest

from src.handler import get_user


class HandlerTest(unittest.TestCase):
    def test_missing_user(self):
        self.assertEqual(get_user("missing"), {"status": 404})

    def test_existing_user(self):
        self.assertEqual(get_user("u1"), {"status": 200, "id": "u1"})


if __name__ == "__main__":
    unittest.main()
