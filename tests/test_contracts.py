"""生态用水履约核算基础契约测试。"""

import unittest

from water_compliance import PermitLedger, unique_by_identity


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {'ledger_code': 'ledger-code-001', 'withdrawal_point': 'withdrawal-point-001', 'permit_revision': 'permit-revision-001', 'state': 'state-001'}

    def test_fingerprint_is_stable(self) -> None:
        left = PermitLedger(**self.values)
        right = PermitLedger(**dict(reversed(list(self.values.items()))))
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_evolve_keeps_original(self) -> None:
        original = PermitLedger(**self.values)
        change_key = next(key for key, value in self.values.items() if isinstance(value, str))
        changed = original.evolve(**{change_key: "revised-value"})
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.assertEqual(getattr(original, change_key), self.values[change_key])

    def test_conflicting_identity_is_rejected(self) -> None:
        first = PermitLedger(**self.values)
        changed_values = dict(self.values)
        change_key = next(key for key in self.values if key != "ledger_code")
        changed_values[change_key] = 2 if isinstance(changed_values[change_key], int) else "conflict"
        second = PermitLedger(**changed_values)
        with self.assertRaises(ValueError):
            unique_by_identity([first, second])


if __name__ == "__main__":
    unittest.main()
