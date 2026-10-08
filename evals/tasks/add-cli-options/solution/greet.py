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


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Greet someone.")
    parser.add_argument("name")
    parser.add_argument("--shout", "-s", action="store_true", help="upper-case the greeting")
    parser.add_argument("--times", "-n", type=positive_int, default=1, help="how many times to greet")
    parser.add_argument("--output", "-o", help="write to this file instead of stdout")
    args = parser.parse_args(argv)

    greeting = f"Hello, {args.name}!"
    if args.shout:
        greeting = greeting.upper()
    text = (greeting + "\n") * args.times
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
