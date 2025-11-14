import argparse
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse
import socket
import json
from collections import deque, defaultdict
import tempfile

# Обход бага с DNS в Windows
socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (args[0], args[1]))]


# =============== ВАЛИДАЦИЯ АРГУМЕНТОВ ===============

def validate_package_name(name: str) -> str:
    if not name or not name.strip():
        raise argparse.ArgumentTypeError("Имя пакета не может быть пустым.")
    return name.strip()


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


# =============== РАБОТА С MAVEN (remote) ===============

def parse_maven_dependencies(pom_content: str) -> list:
    """Извлекает прямые зависимости из pom.xml."""
    dependencies = []
    dep_section_match = re.search(r'<dependencies>(.*?)</dependencies>', pom_content, re.DOTALL | re.IGNORECASE)
    if not dep_section_match:
        return dependencies

    dep_section = dep_section_match.group(1)
    dep_blocks = re.findall(r'<dependency>(.*?)</dependency>', dep_section, re.DOTALL | re.IGNORECASE)

    for block in dep_blocks:
        group_id = re.search(r'<groupId>(.*?)</groupId>', block, re.IGNORECASE)
        artifact_id = re.search(r'<artifactId>(.*?)</artifactId>', block, re.IGNORECASE)
        gid = group_id.group(1).strip() if group_id else None
        aid = artifact_id.group(1).strip() if artifact_id else None
        if gid and aid:
            dependencies.append(f"{gid}:{artifact_id}")

    return dependencies


def fetch_pom_from_remote(repo_url: str, package_name: str) -> str:
    parts = package_name.split(':', 1)
    if len(parts) != 2:
        raise ValueError("Имя пакета должно быть в формате 'groupId:artifactId'.")

    group_id, artifact_id = parts
    group_path = group_id.replace('.', '/')
    base_url = f"{repo_url.rstrip('/')}/{group_path}/{artifact_id}"
    metadata_url = f"{base_url}/maven-metadata.xml"

    # Загрузка metadata
    try:
        with urllib.request.urlopen(metadata_url) as response:
            metadata = response.read().decode('utf-8')
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить maven-metadata.xml из {metadata_url}: {e}")

    # Поиск версии
    version = None
    for pattern in [r'<release>([^<]+)</release>', r'<latest>([^<]+)</latest>']:
        match = re.search(pattern, metadata)
        if match:
            version = match.group(1).strip()
            break

    if not version:
        versions = re.findall(r'<version>([^<]+)</version>', metadata)
        if versions:
            version = versions[-1].strip()

    if not version:
        raise RuntimeError("Не удалось определить версию пакета.")

    # Загрузка pom.xml
    pom_url = f"{base_url}/{version}/{artifact_id}-{version}.pom"
    try:
        with urllib.request.urlopen(pom_url) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"pom.xml не найден: {pom_url}")
        raise RuntimeError(f"HTTP {e.code} при загрузке {pom_url}: {e}")
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить pom.xml: {e}")


# =============== РАБОТА С MOCK (JSON) ===============

def fetch_dependencies_mock(mock_file_path: str, package_name: str) -> list:
    if not os.path.isfile(mock_file_path):
        raise FileNotFoundError(f"Файл не найден: {mock_file_path}")

    try:
        with open(mock_file_path, 'r', encoding='utf-8') as f:
            repo = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON: {e}")

    if package_name not in repo:
        raise ValueError(f"Пакет '{package_name}' отсутствует в репозитории.")

    deps = repo[package_name]
    if not isinstance(deps, list):
        raise ValueError(f"Зависимости должны быть списком.")
    return [str(d).strip() for d in deps if d]


# =============== BFS: ПРЯМОЙ ГРАФ (этап 3) ===============

def bfs_build_dependency_graph(
    start_package: str,
    fetch_deps_func,
    filter_substring: str = ""
) -> dict:
    nodes = set()
    edges = []
    cycles = []
    filtered_out = set()

    depth_map = {start_package: 0}
    queue = deque([(start_package, 0)])
    nodes.add(start_package)

    while queue:
        current, curr_depth = queue.popleft()

        try:
            direct_deps = fetch_deps_func(current)
        except Exception as e:
            print(f"⚠ Пропущен пакет {current}: {e}", file=sys.stderr)
            continue

        for dep in direct_deps:
            if filter_substring and filter_substring in dep:
                filtered_out.add(dep)
                continue

            edges.append((current, dep))

            if dep in depth_map:
                dep_depth = depth_map[dep]
                if dep_depth <= curr_depth:
                    cycles.append([current, dep])
            else:
                depth_map[dep] = curr_depth + 1
                nodes.add(dep)
                queue.append((dep, curr_depth + 1))

    # Уникальные циклы
    unique_cycles = []
    seen = set()
    for cyc in cycles:
        key = frozenset(cyc)
        if key not in seen:
            seen.add(key)
            unique_cycles.append(cyc)

    return {
        'nodes': nodes,
        'edges': edges,
        'cycles': unique_cycles,
        'filtered_out': filtered_out
    }


# =============== BFS: ОБРАТНЫЙ ГРАФ (этап 4) ===============

def bfs_reverse_dependencies(
    target_package: str,
    all_packages: set,
    fetch_deps_func,
    filter_substring: str = ""
) -> dict:
    # Шаг 1: построить полный прямой граф
    forward_edges = []
    filtered_out = set()

    for pkg in all_packages:
        if filter_substring and filter_substring in pkg:
            filtered_out.add(pkg)
            continue
        try:
            deps = fetch_deps_func(pkg)
        except:
            continue
        for dep in deps:
            if filter_substring and filter_substring in dep:
                filtered_out.add(dep)
                continue
            forward_edges.append((pkg, dep))

    # Шаг 2: построить обратный граф
    reverse_adj = defaultdict(list)
    for src, dst in forward_edges:
        reverse_adj[dst].append(src)

    # Шаг 3: BFS от target по обратному графу
    dependents = set()
    visited = set()
    queue = deque()

    if target_package in reverse_adj:
        queue.append(target_package)
        visited.add(target_package)

    while queue:
        current = queue.popleft()
        for depender in reverse_adj.get(current, []):
            if depender not in visited:
                visited.add(depender)
                dependents.add(depender)
                queue.append(depender)

    # Исключаем сам пакет (если самозависимость)
    dependents.discard(target_package)

    # Собираем рёбра зависимостей
    edges = []
    for dep in dependents:
        for src, dst in forward_edges:
            if src == dep and dst == target_package:
                edges.append((dep, target_package))
            # Ищем транзитивные? → достаточно прямых для вывода
    return {
        'dependents': dependents,
        'edges': edges,
        'filtered_out': filtered_out
    }


# =============== ОСНОВНАЯ ФУНКЦИЯ ===============

def main():
    parser = argparse.ArgumentParser(
        description="Инструмент анализа графа зависимостей (этапы 1–4)."
    )
    parser.add_argument('--package', type=validate_package_name, required=True)
    parser.add_argument('--repo-source', type=str, required=True)
    parser.add_argument('--repo-mode', type=validate_repo_mode, required=True)
    parser.add_argument('--output-image', type=validate_output_file, required=True)
    parser.add_argument('--ascii-tree', action='store_true')
    parser.add_argument('--filter', type=str, default='')
    parser.add_argument('--reverse', action='store_true',
                        help='Вывести обратные зависимости (только в mock-режиме)')

    args = parser.parse_args()

    # Валидация источника
    if args.repo_mode == 'remote':
        parsed = urlparse(args.repo_source)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            print("Ошибка: некорректный URL для remote-режима.", file=sys.stderr)
            sys.exit(1)
    elif args.repo_mode == 'mock':
        if not os.path.isfile(args.repo_source):
            print(f"Ошибка: файл не найден: {args.repo_source}", file=sys.stderr)
            sys.exit(1)

    # Вывод параметров
    print("Настроенные параметры:")
    print(f"  package: {args.package}")
    print(f"  repo_source: {args.repo_source}")
    print(f"  repo_mode: {args.repo_mode}")
    print(f"  output_image: {args.output_image}")
    print(f"  filter: '{args.filter}'")
    print(f"  reverse: {args.reverse}\n")

    # Функция получения зависимостей
    if args.repo_mode == 'remote':
        fetch_deps = lambda pkg: parse_maven_dependencies(
            fetch_pom_from_remote(args.repo_source, pkg)
        )
    elif args.repo_mode == 'mock':
        fetch_deps = lambda pkg: fetch_dependencies_mock(args.repo_source, pkg)
    else:
        print("Режим 'local' не поддерживается.", file=sys.stderr)
        sys.exit(1)

    # === Этап 3: прямой граф ===
    try:
        graph = bfs_build_dependency_graph(
            start_package=args.package,
            fetch_deps_func=fetch_deps,
            filter_substring=args.filter
        )
    except Exception as e:
        print(f"Ошибка при построении прямого графа: {e}", file=sys.stderr)
        sys.exit(1)

    print("=== Этап 3: прямые и транзитивные зависимости ===")
    print(f"Узлов: {len(graph['nodes'])}, Рёбер: {len(graph['edges'])}")
    if graph['filtered_out']:
        print("Отфильтровано:", ", ".join(sorted(graph['filtered_out'])))
    print("Рёбра:")
    for src, dst in sorted(graph['edges']):
        print(f"  {src} → {dst}")
    if graph['cycles']:
        print("⚠ Циклы:")
        for i, cyc in enumerate(graph['cycles'], 1):
            print(f"  {i}. {' → '.join(cyc)}")
    else:
        print("Циклов нет.")

    # === Этап 4: обратные зависимости ===
    if args.reverse:
        print("\n=== Этап 4: обратные зависимости ===")
        if args.repo_mode != 'mock':
            print("Обратные зависимости доступны только в --repo-mode=mock.", file=sys.stderr)
            sys.exit(1)

        try:
            with open(args.repo_source, 'r', encoding='utf-8') as f:
                repo_data = json.load(f)
            all_packages = set(repo_data.keys())
        except Exception as e:
            print(f"Ошибка загрузки репозитория: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            rev = bfs_reverse_dependencies(
                target_package=args.package,
                all_packages=all_packages,
                fetch_deps_func=fetch_deps,
                filter_substring=args.filter
            )
        except Exception as e:
            print(f"Ошибка при построении обратного графа: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Пакеты, зависящие от '{args.package}': {len(rev['dependents'])}")
        if rev['dependents']:
            for pkg in sorted(rev['dependents']):
                print(f"  - {pkg}")
        else:
            print("  Нет.")

        if rev['filtered_out']:
            print("Отфильтровано при построении:", ", ".join(sorted(rev['filtered_out'])))

    # === Демо ===
    if args.repo_mode == 'mock' and not args.reverse:
        demo = {"A": ["B", "C"], "B": ["D"], "C": ["D", "E"], "D": [], "E": ["B"]}
        demo_path = os.path.join(tempfile.gettempdir(), "demo_repo.json")
        with open(demo_path, 'w', encoding='utf-8') as f:
            json.dump(demo, f)
        print(f"\n[ДЕМО] Примеры:")
        print(f"  Прямые:   python practica2.py --package A --repo-mode mock --repo-source {demo_path} --output-image x.png")
        print(f"  Обратные: python practica2.py --package B --repo-mode mock --repo-source {demo_path} --output-image x.png --reverse")


if __name__ == "__main__":
    main()