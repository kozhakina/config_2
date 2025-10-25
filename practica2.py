import argparse
import os
import re
import sys
from urllib.parse import urlparse


def validate_package_name(name: str) -> str:
    if not name or not name.strip():
        raise argparse.ArgumentTypeError("Имя пакета не может быть пустым.")
    return name.strip()


def validate_repo_source(source: str) -> str:
    if not source or not source.strip():
        raise argparse.ArgumentTypeError("URL репозитория или путь к файлу не может быть пустым.")
    source = source.strip()
    # Проверяем, похоже ли на URL
    parsed = urlparse(source)
    if parsed.scheme in ('http', 'https'):
        if not parsed.netloc:
            raise argparse.ArgumentTypeError(f"Некорректный URL: {source}")
    else:
        # Считаем, что это локальный путь
        if os.path.sep not in source and not source.startswith(('.', '/')):
            # Просто строка без слешей — возможно, ошибка
            pass  # Разрешаем, т.к. путь может быть относительным без слеша (например, "repo.json")
    return source


def validate_repo_mode(mode: str) -> str:
    allowed = {'local', 'remote', 'mock'}
    if mode not in allowed:
        raise argparse.ArgumentTypeError(f"Режим репозитория должен быть одним из: {', '.join(allowed)}")
    return mode


def validate_output_file(filename: str) -> str:
    if not filename or not filename.strip():
        raise argparse.ArgumentTypeError("Имя файла изображения не может быть пустым.")
    filename = filename.strip()
    # Запрещённые символы в именах файлов (для большинства ОС)
    if re.search(r'[<>:"/\\|?*\x00]', filename):
        raise argparse.ArgumentTypeError("Имя файла содержит недопустимые символы.")
    return filename


def main():
    parser = argparse.ArgumentParser(
        description="Инструмент визуализации графа зависимостей для менеджера пакетов (этап 1)."
    )

    parser.add_argument(
        '--package',
        type=validate_package_name,
        required=True,
        help='Имя анализируемого пакета.'
    )

    parser.add_argument(
        '--repo-source',
        type=validate_repo_source,
        required=True,
        help='URL репозитория или путь к файлу тестового репозитория.'
    )

    parser.add_argument(
        '--repo-mode',
        type=validate_repo_mode,
        required=True,
        help='Режим работы с тестовым репозиторием (local, remote, mock).'
    )

    parser.add_argument(
        '--output-image',
        type=validate_output_file,
        required=True,
        help='Имя сгенерированного файла с изображением графа.'
    )

    parser.add_argument(
        '--ascii-tree',
        action='store_true',
        help='Включить режим вывода зависимостей в формате ASCII-дерева.'
    )

    parser.add_argument(
        '--filter',
        type=str,
        default='',
        help='Подстрока для фильтрации пакетов (может быть пустой).'
    )

    try:
        args = parser.parse_args()
    except SystemExit as e:
        # argparse вызывает sys.exit при ошибках; перехватываем для контроля
        sys.exit(e.code)

    # Вывод всех параметров в формате ключ-значение
    print("Настроенные параметры:")
    print(f"package: {args.package}")
    print(f"repo_source: {args.repo_source}")
    print(f"repo_mode: {args.repo_mode}")
    print(f"output_image: {args.output_image}")
    print(f"ascii_tree: {args.ascii_tree}")
    print(f"filter: {args.filter}")


if __name__ == "__main__":
    main()