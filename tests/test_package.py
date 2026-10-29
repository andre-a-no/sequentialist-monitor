# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import contextlib
import io
import unittest

import sequentialist
import sequentialist.monitor
from sequentialist.core.cli import main


class PackageTest(unittest.TestCase):
    def test_namespace_stays_open(self):
        self.assertIsNone(getattr(sequentialist, "__file__", None))
        self.assertTrue(sequentialist.monitor.__version__)

    def test_subcommand_is_registered(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["monitor"])
        self.assertEqual(code, 2)
        self.assertIn("not a safety instrumented system", " ".join(out.getvalue().split()))


if __name__ == "__main__":
    unittest.main()
