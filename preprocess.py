import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field

import yaml

import nbformat
from box import Box
from rapidfuzz import fuzz


logging.basicConfig(level=logging.INFO)


@dataclass
class PreprocessFilePathConfig:
    assignments_folder: str
    formatted_folder: str
    base_files_folder: str
    base_output_folder: str

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)


@dataclass
class DemosConfig:
    idx_to_names: dict[int, tuple[str, str]]
    idx_to_demo_name: dict[int, str] = field(default_factory=dict)
    idx_to_base_name: dict[int, str] = field(default_factory=dict)
    demo_name_to_idx: dict[str, int] = field(default_factory=dict)
    base_name_to_idx: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.idx_to_demo_name = {k: v[0] for k, v in self.idx_to_names.items()}
        self.idx_to_base_name = {k: v[1] for k, v in self.idx_to_names.items()}
        self.demo_name_to_idx = {v[0]: k for k, v in self.idx_to_names.items()}
        self.base_name_to_idx = {v[1]: k for k, v in self.idx_to_names.items()}

    @classmethod
    def from_dict(cls, d: dict):
        idx_to_names = {
            int(idx): (v['demo_name'], v['base_name'])
            for idx, v in d.items()
        }
        return cls(idx_to_names=idx_to_names)


@dataclass
class PreprocessConfig:
    paths: PreprocessFilePathConfig
    demos: DemosConfig

    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            paths=PreprocessFilePathConfig.from_dict(d['paths']),
            demos=DemosConfig.from_dict(d['demos'])
        )


def _dir_children(*args, folder_only: bool=True) -> list[str]:
    """
    Returns the contents of a directory constructed from the given path components.

    :param args: A variable number of path components that will be joined to form the target directory path.
    :param folder_only: Whether to return only the subfolders, or both subfolders and files.
    :return: List of subdirectories if ``folder_only`` is `True`. List of subdirectories and files if ``folder_only`` is `False`.
    """
    path = os.path.join('', *args)
    all_subdirectories = os.listdir(path)

    if folder_only:
        return [file for file in all_subdirectories if os.path.isdir(os.path.join(path, file))]

    return all_subdirectories


def _is_ipynb(file_path: str) -> bool:
    """
    Returns `True` if ``file_path`` is an IPython notebook file.

    :param file_path: Target file path.
    :return: `True` if ``file_path`` is an IPython notebook file, else `False`.
    """
    return os.path.splitext(file_path)[-1].lower().strip() == '.ipynb'


def _find_latest_version(student_demo_path: str) -> str | None:
    """
    Returns the path to the latest valid version of a student's submission.

    :param student_demo_path: Path to the student's demo submission folder downloaded from SharePoint.
    :return: Path to the latest version folder containing at least one .ipynb file, or None if not found.
    """
    versions = _dir_children(student_demo_path)
    valid_versions = [version for version in versions if any(_is_ipynb(file) for file in _dir_children(student_demo_path, version, folder_only=False))]
    valid_versions = sorted(valid_versions)

    if not valid_versions:
        return None

    graded_version_path = os.path.join(student_demo_path, valid_versions[-1])
    return graded_version_path


def _get_ipynb_paths(graded_version_path: str) -> list[str]:
    """
    Returns a list of .ipynb file paths in the given version folder.

    :param graded_version_path: Path to the folder containing a specific version of the student's submission.
    :return: List of full paths to .ipynb files, or an empty list if none are found.
    """
    ipynb_paths = []
    for file in _dir_children(graded_version_path, folder_only=False):
        if _is_ipynb(file):
            ipynb_paths.append(os.path.join(graded_version_path, file))

    return ipynb_paths


def ipynb_to_py(in_paths: list[str], out_path: str, return_lines: bool=False, skip_lines: list[str]=None, skip_ratio: int=90, remove_comments: bool=False) -> list[str] | None:
    """
    Converts one or more .ipynb notebooks to a .py Python script by extracting code cells.

    :param in_paths: List of input .ipynb file paths to be converted.
    :param out_path: Path where the output .py file will be saved.
    :param return_lines: If True, returns the list of cleaned lines instead of None.
    :param skip_lines: Lines to be skipped if they fuzzily match any line in the notebook.
    :param skip_ratio: Fuzzy match ratio threshold (0–100) for skipping lines.
    :param remove_comments: If True, removes both inline and block comments from the code.
    :return: List of processed lines if return_lines is True, otherwise None.
    """
    with open(out_path, 'w', encoding='utf-8') as out:
        lines = []
        for in_path in in_paths:
            if not _is_ipynb(in_path):
                continue

            with open(in_path, 'r', encoding='utf-8') as ipynb_file:
                try:
                    nb_dict = json.load(ipynb_file)
                    nb_normalized = nbformat.validator.normalize(nb_dict)[1]
                    nb = nbformat.from_dict(nb_normalized)
                except nbformat.reader.NotJSONError:
                    continue
                except UnicodeDecodeError:
                    continue

            output = ''
            for cell in nb.cells:
                if cell.cell_type == 'code' and cell.source:
                    output += ''.join(cell.source)
                output += '\n'

            if remove_comments:
                output = re.sub(r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', '', output)
                output = re.sub(r'#.*', '', output)

            output_lines = output.split('\n')

            for line in output_lines:
                skip_line = False
                if skip_lines:
                    skip_line = any([fuzz.ratio(line, line_to_skip) > skip_ratio for line_to_skip in skip_lines])
                if not skip_line and line.strip():
                    lines.append(line)

            out.write('\n'.join(lines))

    if return_lines:
        return lines


def base_files_to_py(base_in: str, base_out: str, base_name_to_idx: dict[str, int], idx_to_demo_name: dict[int, str]) -> dict[str, list[str]] | None:
    """
    Converts all base `.ipynb` files in a directory to `.py` if they don't already exist,
    and returns a mapping of their names to lists of their lines.

    :param base_in: Path to the directory containing input `.ipynb` base files.
    :param base_out: Path to the directory where converted `.py` files will be saved.
    :param base_name_to_idx: Mapping from raw base file name to its corresponding index or demo number.
    :param idx_to_demo_name: Mapping from index or demo number to its demo name.
    :return: A dictionary mapping demo names to lists of lines of converted Python code.
    """
    try:
        base_lines = {}

        os.makedirs(base_in, exist_ok=True)
        os.makedirs(base_out, exist_ok=True)

        converted_files = set()
        for raw_name in _dir_children(base_in, folder_only=False):
            if raw_name not in base_name_to_idx:
                continue

            try:
                base_num = base_name_to_idx[raw_name]
                base_name = f'Base{base_num}.py'
                in_path = os.path.join(base_in, raw_name)
                out_path = os.path.join(base_out, base_name)

                if not os.path.exists(out_path):
                    base_lines[idx_to_demo_name[base_num]] = ipynb_to_py([in_path], out_path, return_lines=True)
                else:
                    with open(out_path, 'r', encoding='utf-8') as out_file:
                        base_lines[idx_to_demo_name[base_num]] = out_file.readlines()

                converted_files.add((raw_name, base_name))
            except Exception as e:
                logging.error('base_files_to_py()\n'
                              f'Error in converting base files to .py. Kindly ensure that base .ipynb files exist in `{base_in}`')
                sys.exit(1)

        converted_files_message = [f'{raw_name} --> {base_name}' for raw_name, base_name in sorted(list(converted_files))]
        logging.info('base_files_to_py()\n'
                     f'{len(base_lines.keys())} base files converted from .ipynb to .py in `{base_out}`.\n'
                     f'{", ".join(converted_files_message)}\n')

        return base_lines
    except FileNotFoundError:
        logging.error('base_files_to_py()\n'
                      f'Error in converting base files to .py. Kindly ensure that base .ipynb files exist in `{base_in}`\n')
        sys.exit(1)


def handle_student(name: str, assignments_folder: str, formatted_folder: str, demo_name_to_idx: dict[str,int], base_lines: dict[str, list[str]], skip_ratio: int=90) -> None:
    """
    Processes a student's submitted demos by converting the latest .ipynb versions from the SharePoint folder
    to .py files in the specific formatted folder.

    :param name: Name of the student.
    :param assignments_folder: Path to the folder containing raw student submissions downloaded from SharePoint.
    :param formatted_folder: Path to the folder where formatted .py files will be saved.
    :param demo_name_to_idx: Mapping from demo name to index or demo number.
    :param base_lines: Mapping from demo name to list of lines given in the assignment itself.
    :param skip_ratio: Fuzzy match ratio threshold (0–100) for skipping lines.
    :return: None
    """
    try:
        submitted_demos = [demo for demo in _dir_children(assignments_folder, name) if demo in demo_name_to_idx]
        success_demos = set()
        for demo in submitted_demos:
            save_path = os.path.join(formatted_folder, f'Demo{demo_name_to_idx[demo]}')
            file_name = ''.join([char for char in name if char.isalpha() or char == ' '])

            graded_version = _find_latest_version(os.path.join(assignments_folder, name, demo))
            if not graded_version:
                continue

            ipynb_paths = _get_ipynb_paths(graded_version)
            os.makedirs(save_path, exist_ok=True)

            if demo in base_lines:
                ipynb_to_py(ipynb_paths, os.path.join(save_path, f'{file_name}.py'), skip_lines=base_lines[demo], skip_ratio=skip_ratio, remove_comments=True)
            else:
                ipynb_to_py(ipynb_paths, os.path.join(save_path, f'{file_name}.py'), skip_ratio=skip_ratio, remove_comments=True)

            success_demos.add(demo)

        logging.info(f'handle_student():{name}\n'
                     f'{len(success_demos)} submissions found for {name}: {", ".join(sorted(list(success_demos), key=lambda x: demo_name_to_idx[x]))}\n')
    except Exception as e:
        logging.error(f'handle_student():{name}\n'
                      f'Couldn\'t handle submissions for {name}. Kindly check if file structure is in SharePoint format.\n')
        sys.exit(1)


with open('config.yaml', 'r') as f:
    config = Box(yaml.safe_load(f))

preprocess_config = PreprocessConfig.from_dict(config.preprocess)

paths = preprocess_config.paths
demos = preprocess_config.demos

if __name__ == '__main__':
    base = base_files_to_py(paths.base_files_folder, paths.base_output_folder, demos.base_name_to_idx, demos.idx_to_demo_name)

    for student_name in _dir_children(paths.assignments_folder):
        handle_student(student_name, paths.assignments_folder, paths.formatted_folder, demos.demo_name_to_idx, base)