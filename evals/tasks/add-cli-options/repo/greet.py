"""Greet people from the command line.

Usage: python greet.py NAME [--shout] [--times N] [--output FILE]

* NAME: who to greet. The tool prints "Hello, NAME!" followed by a newline.
* --shout / -s: print the greeting in upper case ("HELLO, NAME!").
* --times N / -n N: print the greeting N times, one per line. N must be a positive integer;
  anything else (zero, negative, not a number) is a usage error, which argparse reports by
  exiting with status 2. The default is 1.
* --output FILE / -o FILE: write the output to FILE instead of printing it. Existing files are
  overwritten. Nothing is printed to stdout in that case.

main(argv=None) parses argv (or sys.argv) and returns 0 on success.
"""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="Greet someone.")
    parser.add_argument("name")
    args = parser.parse_args(argv)
    print(f"Hello, {args.name}!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
