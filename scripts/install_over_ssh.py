import argparse
import pathlib
import shlex
import sys

import paramiko


def build_remote_command(args):
    env = {
        "REPO_OWNER": args.repo_owner,
        "REPO_NAME": args.repo_name,
        "REPO_REF": args.repo_ref,
        "DEST_ROOT": args.dest_root,
    }
    if args.backup_root:
        env["BACKUP_ROOT"] = args.backup_root

    env_prefix = " ".join(
        "{0}={1}".format(key, shlex.quote(value))
        for key, value in env.items()
    )
    app_args = " ".join(shlex.quote(value) for value in args.apps)
    return "{0} sh -s -- {1}".format(env_prefix, app_args)


def main():
    parser = argparse.ArgumentParser(
        description="Install NanoKVM Pro DIY apps to a NanoKVM device over SSH."
    )
    parser.add_argument("--host", required=True, help="NanoKVM IP or hostname")
    parser.add_argument("--user", default="root", help="SSH username")
    parser.add_argument("--password", required=True, help="SSH password")
    parser.add_argument(
        "--repo-owner", default="vadlike", help="GitHub owner with the apps repo"
    )
    parser.add_argument(
        "--repo-name", default="NanoKVM-Pro-DIY-APPS", help="GitHub repo name"
    )
    parser.add_argument(
        "--repo-ref", default="main", help="Git reference: branch, tag, or commit"
    )
    parser.add_argument(
        "--dest-root",
        default="/userapp",
        help="Destination root on NanoKVM, default: /userapp",
    )
    parser.add_argument(
        "--backup-root",
        default="",
        help="Optional backup root on NanoKVM",
    )
    parser.add_argument(
        "apps",
        nargs="+",
        help="One or more app names, or 'all'",
    )
    args = parser.parse_args()

    script_path = pathlib.Path(__file__).with_name("install-userapp.sh")
    script_text = script_path.read_text(encoding="utf-8")
    remote_command = build_remote_command(args)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=args.host,
            username=args.user,
            password=args.password,
            timeout=20,
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=600)
        stdin.write(script_text)
        stdin.channel.shutdown_write()

        out_text = stdout.read().decode("utf-8", "replace")
        err_text = stderr.read().decode("utf-8", "replace")
        exit_code = stdout.channel.recv_exit_status()

        if out_text:
            sys.stdout.write(out_text)
        if err_text:
            sys.stderr.write(err_text)

        raise SystemExit(exit_code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
