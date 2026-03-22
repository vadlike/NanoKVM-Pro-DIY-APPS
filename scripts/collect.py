import re
from collections import OrderedDict
from pathlib import Path

OUTFILE_NAME = "apps.toml"
ROOT_DIR = (Path(__file__).resolve().parent.parent / "apps").resolve()
OUTPUT_FILE = Path(__file__).resolve().parent / OUTFILE_NAME

DEFAULT_APP = {
    "application_name": "",
    "application_version": "1.0.0",
    "application_descriptions": "No description provided",
    "author_name": "Unknown",
    "interaction_requires_user_input": False,
}

SEMVER_REGEX = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

EXCLUDED_FILE_NAMES = {
    "config.json",
    "README.md",
    "README.txt",
    "logo.png",
    "logo.jpg",
    "logo.jpeg",
    "logo.gif",
}

EXCLUDED_DIR_NAMES = {
    "__pycache__",
}


def should_include_file(path: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return False
    if path.name in EXCLUDED_FILE_NAMES:
        return False
    return True


def parse_scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def parse_simple_toml(path: Path):
    data = {}
    current_section = None

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "#" in line:
                line = line.split("#", 1)[0].rstrip()
                if not line:
                    continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].strip()
                if current_section and current_section not in data:
                    data[current_section] = {}
                continue
            if "=" not in line or not current_section:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            data[current_section][key] = parse_scalar(value)

    return data


def toml_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_bool(value):
    return "true" if value else "false"


def toml_list(values):
    rendered = []
    for value in values:
        rendered.append(toml_string(value))
    return "[ {0},]".format(", ".join(rendered))


def dump_apps_toml(path: Path, apps):
    lines = []
    for app in apps:
        lines.append("[[apps]]")
        lines.append("folder = {0}".format(toml_string(app["folder"])))
        lines.append(
            "application_name = {0}".format(toml_string(app["application_name"]))
        )
        lines.append(
            "application_version = {0}".format(
                toml_string(app["application_version"])
            )
        )
        lines.append(
            "application_descriptions = {0}".format(
                toml_string(app["application_descriptions"])
            )
        )
        lines.append("author_name = {0}".format(toml_string(app["author_name"])))
        lines.append(
            "interaction_requires_user_input = {0}".format(
                toml_bool(app["interaction_requires_user_input"])
            )
        )
        lines.append("files = {0}".format(toml_list(app["files"])))
        lines.append("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


apps_list = []

for folder in sorted(ROOT_DIR.iterdir()):
    if not folder.is_dir():
        continue

    folder_name = folder.name
    app_info = OrderedDict()
    app_info["folder"] = folder_name
    app_info.update(DEFAULT_APP)
    app_info["application_name"] = folder_name

    files = []
    for file_path in sorted(folder.rglob("*")):
        if not file_path.is_file():
            continue
        if not should_include_file(file_path.relative_to(ROOT_DIR)):
            continue
        rel_path = file_path.relative_to(ROOT_DIR).as_posix()
        files.append(rel_path)

    app_info["files"] = files

    app_toml_path = folder / "app.toml"
    if app_toml_path.is_file():
        try:
            data = parse_simple_toml(app_toml_path)

            if "application" in data:
                app_name_tmp = data["application"].get("name", folder_name)
                if app_info["application_name"] != app_name_tmp:
                    raise RuntimeError(
                        "The folder name {0} does not match the app name {1}".format(
                            folder_name, app_name_tmp
                        )
                    )

                version = data["application"].get("version", "1.0.0")
                if not SEMVER_REGEX.match(version):
                    print(
                        "Warning: {0} version '{1}' is not SemVer, skipped.".format(
                            folder_name, version
                        )
                    )
                    continue

                app_info["application_version"] = version
                app_info["application_descriptions"] = data["application"].get(
                    "descriptions", "No description provided"
                )

            if "author" in data:
                app_info["author_name"] = data["author"].get("name", "Unknown")

            if "interaction" in data:
                app_info["interaction_requires_user_input"] = data[
                    "interaction"
                ].get("requires_user_input", False)
        except Exception as exc:
            print(
                "Warning: Failed to parse {0}: {1}".format(app_toml_path, exc)
            )

    apps_list.append(app_info)

dump_apps_toml(OUTPUT_FILE, apps_list)

print("Generated {0} with {1} apps.".format(OUTPUT_FILE, len(apps_list)))
