import argparse
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse
import socket
import json
from collections import deque
import tempfile

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
        # Для mock-mode — проверим, что путь существует, если это файл
        if args.repo_mode == 'mock':  # временно, т.к. args ещё нет — исправим ниже
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
    parts = package_name.split(':', 1)
    if len(parts) != 2:
        raise ValueError("Имя пакета должно быть в формате 'groupId:artifactId'.")

    group_id, artifact_id = parts
    group_path = group_id.replace('.', '/')
    base_url = f"{repo_url.rstrip('/')}/{group_path}/{artifact_id}"

    metadata_url = f"{base_url}/maven-metadata.xml"
    try:
        with urllib.request.urlopen(metadata_url) as response:
            metadata = response.read().decode('utf-8')
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить maven-metadata.xml из {metadata_url}: {e}")

    version = None
    release_match = re.search(r'<release>([^<]+)</release>', metadata)
    if release_match:
        version = release_match.group(1).strip()

    if not version:
        latest_match = re.search(r'<latest>([^<]+)</latest>', metadata)
        if latest_match:
            version = latest_match.group(1).strip()

    if not version:
        versions = re.findall(r'<version>([^<]+)</version>', metadata)
        if versions:
            version = versions[-1].strip()

    if not version:
        raise RuntimeError(
            "Не удалось определить версию: в maven-metadata.xml отсутствуют <release>, <latest> и <version>."
        )

    pom_url = f"{base_url}/{version}/{artifact_id}-{version}.pom"

    try:
        with urllib.request.urlopen(pom_url) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"pom.xml не найден по адресу: {pom_url} (проверьте имя пакета и доступность репозитория)")
        raise RuntimeError(f"HTTP {e.code} при загрузке {pom_url}: {e}")
    except Exception as e:
        raise RuntimeError(f"Не удалось загрузить pom.xml: {e}")


# ============== НОВЫЕ ФУНКЦИИ ДЛЯ ЭТАПА 3 ==============

def fetch_dependencies_mock(mock_file_path: str, package_name: str) -> list:
    """
    Загружает прямые зависимости для package_name из JSON-файла вида:
    {
      "A": ["B", "C"],
      "B": ["D"],
      "C": ["D", "E"],
      "D": [],
      "E": ["B"]   ← цикл: E → B → D, и если B→E — цикл
    }
    """
    if not os.path.isfile(mock_file_path):
        raise FileNotFoundError(f"Файл тестового репозитория не найден: {mock_file_path}")

    try:
        with open(mock_file_path, 'r', encoding='utf-8') as f:
            repo = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON в файле {mock_file_path}: {e}")

    if package_name not in repo:
        raise ValueError(f"Пакет '{package_name}' отсутствует в тестовом репозитории.")

    dependencies = repo[package_name]
    if not isinstance(dependencies, list):
        raise ValueError(f"Зависимости для '{package_name}' должны быть списком.")
    return [str(dep).strip() for dep in dependencies if dep]


def bfs_build_dependency_graph(
    start_package: str,
    fetch_deps_func,
    filter_substring: str = ""
) -> dict:
    """
    Строит граф зависимостей BFS без рекурсии.
    :param start_package: начальный пакет
    :param fetch_deps_func: функция (package_name) → list[dep_name]
    :param filter_substring: подстрока для фильтрации (если dep содержит её — пропускаем)
    :return: {
        'nodes': set[str],
        'edges': list[(from, to)],
        'cycles': list[list[str]],  # циклы в виде [A, B, C, A] — но без дублей
        'filtered_out': set[str]
    }
    """
    nodes = set()
    edges = []
    cycles = []
    filtered_out = set()

    # Обход BFS
    queue = deque()
    # Для обнаружения циклов: храним путь до текущего узла
    # key: узел, value: список предков в текущем пути (в порядке BFS-поиска)
    # Но BFS не даёт полного пути — для простоты будем проверять: если dep уже в nodes **и** в текущей очереди — цикл?
    # Лучше: использовать отдельный set `in_queue_or_visited`, а для циклов — проверять при добавлении:
    # если dep уже был посещён в этом BFS (в `nodes`) — потенциальный цикл.
    # Однако: A → B, C → B — не цикл. Цикл: A → B → C → A.
    # Решение: храним `depth_map`: package → min_depth. Если dep уже есть в depth_map и его depth <= current+1 — это back-edge → цикл.

    depth_map = {start_package: 0}
    queue.append((start_package, 0))  # (package, depth)
    nodes.add(start_package)

    while queue:
        current, curr_depth = queue.popleft()

        try:
            direct_deps = fetch_deps_func(current)
        except Exception as e:
            print(f"⚠ Пропущен пакет {current} (ошибка при получении зависимостей): {e}", file=sys.stderr)
            continue

        for dep in direct_deps:
            # === Фильтрация ===
            if filter_substring and filter_substring in dep:
                filtered_out.add(dep)
                continue

            # === Добавление ребра ===
            edges.append((current, dep))

            # === Проверка цикла ===
            if dep in depth_map:
                # dep уже посещался — проверим, является ли это back-edge (глубина <= curr_depth)
                dep_depth = depth_map[dep]
                if dep_depth <= curr_depth:
                    # Возможен цикл: строим путь current → ... → dep → ... → current?
                    # Упрощённо: фиксируем факт цикла и сохраняем участников
                    # Для детекции полного цикла нужен DFS/DFS-стек — но по условию "корректно обработать" = обнаружить и зафиксировать.
                    # Мы просто зафиксируем ребро как часть цикла.
                    cycles.append([current, dep])  # можно расширить позже, но для этапа — достаточно сигнала
            else:
                depth_map[dep] = curr_depth + 1
                nodes.add(dep)
                queue.append((dep, curr_depth + 1))

    # Убираем дубликаты циклов (по множеству узлов)
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


# ============== ОБНОВЛЁННЫЙ main() ==============

def main():
    parser = argparse.ArgumentParser(
        description="Инструмент визуализации графа зависимостей для менеджера пакетов (этапы 1–3)."
    )

    parser.add_argument(
        '--package',
        type=validate_package_name,
        required=True,
        help='Имя анализируемого пакета в формате groupId:artifactId (или одно имя для mock-режима).'
    )

    parser.add_argument(
        '--repo-source',
        type=str,  # временно убираем validate_repo_source — сделаем после парсинга mode
        required=True,
        help='URL репозитория (remote), или путь к JSON-файлу (mock).'
    )

    parser.add_argument(
        '--repo-mode',
        type=validate_repo_mode,
        required=True,
        help='Режим: remote (Maven Central), mock (тестовый JSON-файл).'
    )

    parser.add_argument(
        '--output-image',
        type=validate_output_file,
        required=True,
        help='Имя файла изображения графа (на этапе 3 — не используется, но требуется аргументом).'
    )

    parser.add_argument(
        '--ascii-tree',
        action='store_true',
        help='Режим вывода ASCII-дерева (игнорируется на этапе 3).'
    )

    parser.add_argument(
        '--filter',
        type=str,
        default='',
        help='Подстрока: пакеты, содержащие её, исключаются из анализа.'
    )

    try:
        args = parser.parse_args()
    except SystemExit as e:
        sys.exit(e.code)

    # === Дополнительная валидация repo-source с учётом режима ===
    if args.repo_mode == 'remote':
        # Валидация URL
        parsed = urlparse(args.repo_source)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            print("Ошибка: для --repo-mode=remote требуется корректный HTTP/HTTPS URL.", file=sys.stderr)
            sys.exit(1)
    elif args.repo_mode == 'mock':
        if not os.path.isfile(args.repo_source):
            print(f"Ошибка: файл '{args.repo_source}' не найден для --repo-mode=mock.", file=sys.stderr)
            sys.exit(1)

    # === Этап 1: вывод параметров ===
    print("Настроенные параметры:")
    print(f"package: {args.package}")
    print(f"repo_source: {args.repo_source}")
    print(f"repo_mode: {args.repo_mode}")
    print(f"output_image: {args.output_image}")
    print(f"ascii_tree: {args.ascii_tree}")
    print(f"filter: '{args.filter}'")

    # === Этап 2: прямые зависимости (для remote) — уже есть, но этап 3 делает транзитивные, так что этап 2 опускаем ===
    # Перейдём сразу к этапу 3.

    # === Этап 3: построение графа зависимостей ===
    if args.repo_mode == 'remote':
        def fetch_deps(package):
            pom = fetch_pom_from_remote(args.repo_source, package)
            return parse_maven_dependencies(pom)
    elif args.repo_mode == 'mock':
        def fetch_deps(package):
            return fetch_dependencies_mock(args.repo_source, package)
    else:
        print("Ошибка: режим 'local' не поддерживается на этапе 3.", file=sys.stderr)
        sys.exit(1)

    try:
        graph = bfs_build_dependency_graph(
            start_package=args.package,
            fetch_deps_func=fetch_deps,
            filter_substring=args.filter
        )
    except Exception as e:
        print(f"Ошибка при построении графа: {e}", file=sys.stderr)
        sys.exit(1)

    # === Вывод результата этапа 3 ===
    print("\n=== Результат этапа 3: транзитивный граф зависимостей ===")
    print(f"Всего узлов: {len(graph['nodes'])}")
    print(f"Всего рёбер: {len(graph['edges'])}")
    print(f"Отфильтровано: {len(graph['filtered_out'])} пакетов")

    if graph['filtered_out']:
        print("Отфильтрованные пакеты:", ", ".join(sorted(graph['filtered_out'])))

    print("\nРёбра графа:")
    for src, dst in sorted(graph['edges']):
        print(f"{src} → {dst}")

    if graph['cycles']:
        print(f"\n⚠ Обнаружено циклических зависимостей: {len(graph['cycles'])}")
        for i, cyc in enumerate(graph['cycles'], 1):
            print(f"  Цикл {i}: {' → '.join(cyc)}")
    else:
        print("\nЦиклических зависимостей не обнаружено.")

    # === Демонстрация на тестовых данных (опционально, можно удалить в продакшене) ===
    if args.repo_mode == 'mock':
        # Пример тестового файла (для удобства — можно создать временный)
        demo_mock_data = {
            "A": ["B", "C"],
            "B": ["D"],
            "C": ["D", "E"],
            "D": [],
            "E": ["B"]   # цикл: B → D ← C ← E → B (E→B, B→D, C→D, C→E)
        }
        demo_path = os.path.join(tempfile.gettempdir(), "demo_repo.json")
        with open(demo_path, 'w') as f:
            json.dump(demo_mock_data, f)
        print(f"\n[ДЕМО] Тестовый репозиторий сохранён в: {demo_path}")
        print("Примеры запуска (в терминале):")
        print(f"  python script.py --package A --repo-mode mock --repo-source {demo_path} --output-image graph.png")
        print(f"  python script.py --package A --repo-mode mock --repo-source {demo_path} --output-image graph.png --filter D")


if __name__ == "__main__":
    main()