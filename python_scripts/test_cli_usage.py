"""The CLI's command list has one source: the argparse parser. Every subcommand has a help line and a handler."""
import argparse
import io
import contextlib
import unittest

import dev_ticket


def subcommands() -> dict:
    parser = dev_ticket.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


class CliUsageTests(unittest.TestCase):
    def test_every_subcommand_has_a_handler_and_a_help_line(self):
        names = set(subcommands())
        self.assertEqual(names, set(dev_ticket.HANDLERS))
        self.assertEqual(names, set(dev_ticket.COMMAND_HELP))

    def test_no_api_list_names_real_subcommands(self):
        self.assertFalse(set(dev_ticket.NO_API) - set(subcommands()))

    def test_top_level_help_lists_every_subcommand(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            dev_ticket.build_parser().parse_args(["-h"])
        text = out.getvalue()
        for name in subcommands():
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
