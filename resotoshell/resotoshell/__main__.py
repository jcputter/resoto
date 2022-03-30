import os
import pathlib
import sys
from threading import Event
from typing import Callable
from urllib.parse import urlencode

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from resotolib.args import ArgumentParser, Namespace
from resotolib.core.ca import ensure_tls_setup_for_requests
from resotolib.jwt import add_args as jwt_add_args
from resotolib.logging import log, setup_logger, add_args as logging_add_args
from resotolib.utils import rnd_str
from resotoshell.shell import Shell
from rich.console import Console


def main() -> None:
    setup_logger("resotoshell")
    shutdown_event = Event()
    arg_parser = ArgumentParser(
        description="resoto shell", env_args_prefix="RESOTOSHELL_"
    )
    add_args(arg_parser)
    logging_add_args(arg_parser)
    jwt_add_args(arg_parser)
    args = arg_parser.parse_args()

    ensure_tls_setup_for_requests(args.resotocore_uri, args.psk, args.tls_cert)
    headers = {"Accept": "text/plain"}
    execute_endpoint = f"{args.resotocore_uri}/cli/execute"
    execute_endpoint += f"?resoto_session_id={rnd_str()}"
    if args.resotocore_graph:
        query_string = urlencode({"graph": args.resotocore_graph})
        execute_endpoint += f"&{query_string}"
    if args.resotocore_section:
        query_string = urlencode({"section": args.resotocore_section})
        execute_endpoint += f"&{query_string}"

    if ArgumentParser.args.stdin:
        shell = Shell(execute_endpoint, False, "monochrome")
        log.debug("Reading commands from STDIN")
        try:
            for command in sys.stdin.readlines():
                command = command.rstrip()
                shell.handle_command(command, headers)
        except KeyboardInterrupt:
            pass
        except (RuntimeError, ValueError) as e:
            log.error(e)
        except Exception:
            log.exception("Caught unhandled exception while processing CLI command")
        finally:
            shutdown_event.set()
    else:
        shell = Shell(execute_endpoint, True, detect_color_system(args))
        completer = None
        history_file = str(pathlib.Path.home() / ".resotoshell_history")
        history = FileHistory(history_file)
        session = PromptSession(history=history)
        log.debug("Starting interactive session")
        # send the welcome command to the core
        shell.handle_command("welcome", headers)

        while not shutdown_event.is_set():
            try:
                command = session.prompt("> ", completer=completer)
                if command == "":
                    continue
                if command == "quit":
                    shutdown_event.set()
                    continue

                shell.handle_command(command, headers)

            except KeyboardInterrupt:
                pass
            except EOFError:
                shutdown_event.set()
            except (RuntimeError, ValueError) as e:
                log.error(e)
            except Exception:
                log.exception("Caught unhandled exception while processing CLI command")

    sys.exit(0)


def detect_color_system(args: Namespace) -> str:
    if args.no_color:
        return "monochrome"
    else:
        lookup = {
            None: "monochrome",
            "standard": "standard",
            "256": "eight_bit",
            "truecolor": "truecolor",
            "windows": "legacy_windows",
        }
        cs = lookup.get(Console().color_system, "standard")
        log.debug(f"Detected color system is: {cs}")
        return cs


def add_args(arg_parser: ArgumentParser) -> None:
    def is_file(message: str) -> Callable[[str], str]:
        def check_file(path: str) -> str:
            if os.path.isfile(path):
                return path
            else:
                raise AttributeError(f"{message}: path {path} is not a directory!")

        return check_file

    arg_parser.add_argument(
        "--resotocore-uri",
        help="resotocore URI (default: https://localhost:8900)",
        default="https://localhost:8900",
        dest="resotocore_uri",
    )
    arg_parser.add_argument(
        "--tls-cert",
        type=is_file("can not parse --tls-cert"),
        help="Path to a single file in PEM format containing the certificate as well as any number "
        "of CA certificates needed to establish the certificate’s authenticity.",
    )
    arg_parser.add_argument(
        "--resotocore-section",
        help="All queries are interpreted with this section name. If not set, the server default is used.",
        dest="resotocore_section",
    )
    arg_parser.add_argument(
        "--resotocore-graph",
        help="The name of the graph to use by default. If not set, the server default is used.",
        dest="resotocore_graph",
    )
    arg_parser.add_argument(
        "--download-directory",
        help="If files are received, they are written to this directory.",
        default=".",
        dest="download_directory",
    )
    arg_parser.add_argument(
        "--no-color",
        help="Output should be rendered plain without any color escape sequences.",
        dest="no_color",
        action="store_true",
        default=False,
    )
    arg_parser.add_argument(
        "--stdin",
        help="Read from STDIN instead of opening a shell",
        dest="stdin",
        action="store_true",
        default=False,
    )


if __name__ == "__main__":
    main()
