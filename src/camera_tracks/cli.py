"""Command line entry point.

The original single-dataset invocation is unchanged::

    camera-tracks --source INPUT --output REPORT

Additional subcommands provide the offline calibration/delivery workflow::

    camera-tracks build   --input BUILD.json --store UPSTREAM_STATE.json
    camera-tracks correct --store UPSTREAM_STATE.json --request CHANGE.json
    camera-tracks query   --store UPSTREAM_STATE.json --version N \
                          --sequence S --track TRACK
    camera-tracks export  --store UPSTREAM_STATE.json --output PACKAGE.json \
                          (--full | --base-version N)
    camera-tracks apply   --store DOWNSTREAM_STATE.json --package PACKAGE.json
    camera-tracks view    --store DOWNSTREAM_STATE.json [--output VIEW.json]
    camera-tracks view    --store DOWNSTREAM_STATE.json --sequence S --track TRACK
    camera-tracks accept  --baseline VIEW.json --resurvey VIEW.json \
                          --config ZONE.json --output REPORT.json

All commands print one JSON object to standard output on success and a
readable ``error: ...`` line to standard error with exit status 2 on failure.
"""

import argparse
import json
import sys

from . import accept, downstream, io, upstream
from .service import process

SUBCOMMANDS = ("build", "correct", "query", "export", "apply", "view", "accept")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _legacy(argv):
    parser = _Parser(prog="camera-tracks")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = process(io.load(args.source))
    io.publish_new(args.output, io.dump_store(result))
    _emit(result["summary"])
    return 0


def _build(argv):
    parser = _Parser(prog="camera-tracks build")
    parser.add_argument("--input", required=True)
    parser.add_argument("--store", required=True)
    args = parser.parse_args(argv)
    _emit(upstream.build(args.input, args.store))
    return 0


def _correct(argv):
    parser = _Parser(prog="camera-tracks correct")
    parser.add_argument("--store", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    _emit(upstream.correct(args.store, args.request))
    return 0


def _query(argv):
    parser = _Parser(prog="camera-tracks query")
    parser.add_argument("--store", required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--track", required=True)
    args = parser.parse_args(argv)
    _emit(upstream.query(args.store, args.version, args.sequence, args.track))
    return 0


def _export(argv):
    parser = _Parser(prog="camera-tracks export")
    parser.add_argument("--store", required=True)
    parser.add_argument("--output", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--full", action="store_true")
    group.add_argument("--base-version", type=int)
    args = parser.parse_args(argv)
    _emit(
        upstream.export(
            args.store, args.output, full=args.full, base_version=args.base_version
        )
    )
    return 0


def _apply(argv):
    parser = _Parser(prog="camera-tracks apply")
    parser.add_argument("--store", required=True)
    parser.add_argument("--package", required=True)
    args = parser.parse_args(argv)
    _emit(downstream.apply(args.store, args.package))
    return 0


def _view(argv):
    parser = _Parser(prog="camera-tracks view")
    parser.add_argument("--store", required=True)
    parser.add_argument("--output")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--track")
    args = parser.parse_args(argv)
    if (args.sequence is None) != (args.track is None):
        raise ValueError("--sequence and --track must be given together")
    if args.sequence is not None:
        if args.output is not None:
            raise ValueError("point queries are printed, not written to --output")
        _emit(downstream.query(args.store, args.sequence, args.track))
    else:
        _emit(downstream.export_view(args.store, args.output))
    return 0


def _accept(argv):
    parser = _Parser(prog="camera-tracks accept")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--resurvey", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    _emit(accept.run(args.baseline, args.resurvey, args.config, args.output))
    return 0


_HANDLERS = {
    "build": _build,
    "correct": _correct,
    "query": _query,
    "export": _export,
    "apply": _apply,
    "view": _view,
    "accept": _accept,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in SUBCOMMANDS:
            return _HANDLERS[argv[0]](argv[1:])
        if argv and argv[0] in ("-h", "--help"):
            print(
                "usage: camera-tracks --source INPUT --output REPORT\n"
                "       camera-tracks {build,correct,query,export,apply,view,accept} ..."
            )
            return 0
        return _legacy(argv)
    except KeyError as error:
        print(f"error: missing field or value: {error}", file=sys.stderr)
        return 2
    except (
        ValueError,
        OSError,
        UnicodeError,
        RecursionError,
        RuntimeError,
        TypeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
