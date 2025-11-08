import argparse
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse
import socket
socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (args[0], args[1]))]


def validate_package_name(name: str) -> str:
    if not name or not name.strip():
        raise argparse.ArgumentTypeError("Имя пакета не может быть пустым.")
    return name.strip()


def validate_repo_source(source: str) -> str:
    if not source or not source.strip():
        raise argparse.ArgumentTypeError("URL репозитория или путь к файлу не может быть пустым.")
    source = source.strip()
    parsed = urlparse(source)
    if parsed.scheme in ('http', 'https'):
        if not parsed.netloc:
            raise argparse.ArgumentTypeError(f"Некорректный URL: {source}")
    else:
        # Для этапа 2 разрешаем только remote, но валидация оставлена общей
        pass
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
    if re.search(r'[<>:"/\\|?*\x00]', filename):
        raise argparse.ArgumentTypeError("Имя файла содержит недопустимые символы.")
    return filename


def parse_maven_dependencies(pom_content: str) -> list:
    """Извлекает прямые зависимости из содержимого pom.xml (только <dependencies>, не <dependencyManagement>)."""
    dependencies = []
    # Ищем секцию <dependencies> (игнорируем <dependencyManagement>)
    dep_section_match = re.search(r'<dependencies>(.*?)</dependencies>', pom_content, re.DOTALL | re.IGNORECASE)
    if not dep_section_match:
        return dependencies

    dep_section = dep_section_match.group(1)
    dep_blocks = re.findall(r'<dependency>(.*?)</dependency>', dep_section, re.DOTALL | re.IGNORECASE)

    for block in dep_blocks:
        group_id_match = re.search(r'<groupId>(.*?)</groupId>', block, re.IGNORECASE)
        artifact_id_match = re.search(r'<artifactId>(.*?)</artifactId>', block, re.IGNORECASE)

        group_id = group_id_match.group(1).strip() if group_id_match else None
        artifact_id = artifact_id_match.group(1).strip() if artifact_id_match else None

        if group_id and artifact_id:
            dependencies.append(f"{group_id}:{artifact_id}")

    return dependencies


def fetch_pom_from_remote(repo_url: str, package_name: str) -> str:
    """Загружает pom.xml для заданного пакета из удалённого Maven-репозитория (Maven Central layout)."""
    parts = package_name.split(':', 1)
    if len(parts) != 2:
        raise ValueError("Имя пакета должно быть в формате 'groupId:artifactId'.")

    group_id, artifact_id = parts
    group_path = group_id.replace('.', '/')
    base_url = f"{repo_url.rstrip('/')}/{group_path}/{artifact_id}"

    # Шаг 1: Загрузка maven-metadata.xml
    metadata_url = f"{base_url}/maven-metadata.xml"
    try:
        with urllib.request.urlopen(metadata_url) as response:
            metadata = response.read().decode('utf-8')
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить maven-metadata.xml из {metadata_url}: {e}")

    # Шаг 2: Извлечение версии — приоритет: <release> → <latest> → последняя <version>
    version = None

    # Попытка 1: <release>
    release_match = re.search(r'<release>([^<]+)</release>', metadata)
    if release_match:
        version = release_match.group(1).strip()

    # Попытка 2: <latest>
    if not version:
        latest_match = re.search(r'<latest>([^<]+)</latest>', metadata)
        if latest_match:
            version = latest_match.group(1).strip()

    # Попытка 3: последняя <version> в списке
    if not version:
        versions = re.findall(r'<version>([^<]+)</version>', metadata)
        if versions:
            version = versions[-1].strip()  # metadata.xml обычно сортирует версии по возрастанию

    if not version:
        raise RuntimeError(
            "Не удалось определить версию: в maven-metadata.xml отсутствуют <release>, <latest> и <version>."
        )

    # Шаг 3: Формирование URL к pom.xml
    pom_url = f"{base_url}/{version}/{artifact_id}-{version}.pom"

    # Шаг 4: Загрузка pom.xml
    try:
        with urllib.request.urlopen(pom_url) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"pom.xml не найден по адресу: {pom_url} (проверьте имя пакета и доступность репозитория)")
        raise RuntimeError(f"HTTP {e.code} при загрузке {pom_url}: {e}")
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить pom.xml: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Инструмент визуализации графа зависимостей для менеджера пакетов (этапы 1 и 2)."
    )

    parser.add_argument(
        '--package',
        type=validate_package_name,
        required=True,
        help='Имя анализируемого пакета в формате groupId:artifactId.'
    )

    parser.add_argument(
        '--repo-source',
        type=validate_repo_source,
        required=True,
        help='URL репозитория (для режима remote — например, https://repo1.maven.org/maven2).'
    )

    parser.add_argument(
        '--repo-mode',
        type=validate_repo_mode,
        required=True,
        help='Режим работы с репозиторием (этап 2 поддерживает только "remote").'
    )

    parser.add_argument(
        '--output-image',
        type=validate_output_file,
        required=True,
        help='Имя файла изображения графа (для этапа 2 — требуется, но не используется в этом этапе).'
    )

    parser.add_argument(
        '--ascii-tree',
        action='store_true',
        help='Режим вывода ASCII-дерева (игнорируется на этапе 2).'
    )

    parser.add_argument(
        '--filter',
        type=str,
        default='',
        help='Подстрока для фильтрации зависимостей (игнорируется на этапе 2).'
    )

    try:
        args = parser.parse_args()
    except SystemExit as e:
        sys.exit(e.code)

    # === Этап 1: вывод настроенных параметров ===
    print("Настроенные параметры:")
    print(f"package: {args.package}")
    print(f"repo_source: {args.repo_source}")
    print(f"repo_mode: {args.repo_mode}")
    print(f"output_image: {args.output_image}")
    print(f"ascii_tree: {args.ascii_tree}")
    print(f"filter: {args.filter}")

    # === Этап 2: сбор данных о прямых зависимостях (только remote) ===
    if args.repo_mode != 'remote':
        print("Ошибка: Этап 2 поддерживает только --repo-mode=remote.", file=sys.stderr)
        sys.exit(1)

    try:
        pom_content = fetch_pom_from_remote(args.repo_source, args.package)
        dependencies = parse_maven_dependencies(pom_content)
    except Exception as e:
        print(f"Ошибка при получении зависимостей: {e}", file=sys.stderr)
        sys.exit(1)

    # Вывод всех прямых зависимостей — требование п.3 этапа 2
    for dep in dependencies:
        print(dep)


if __name__ == "__main__":
    main()