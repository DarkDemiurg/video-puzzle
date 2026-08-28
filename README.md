# Video Puzzle

[![CI](https://github.com/DarkDemiurg/video-puzzle/actions/workflows/ci.yml/badge.svg)](https://github.com/DarkDemiurg/video-puzzle/actions/workflows/ci.yml)

Desktop app that tiles videos into a mosaic — classic **puzzle** layouts or a **video wall** — and encodes with `ffmpeg`.

Десктопное приложение на Python (PySide6): склейка роликов в мозаику или видеостену. Можно сохранить bash-скрипт или сразу запустить `ffmpeg` из интерфейса.

## Возможности

**Пазл** (2–4 ролика)

- Схемы: 2 горизонтально / вертикально, широкая или узкая пирамида, квадрат 2×2
- Общая шкала: один момент во все превью
- Необязательный фрагмент выхода (например с 20 по 60 с)
- Синхронизация по звуку (оценка сдвига в начале или в хвосте)
- Звук берётся с первого файла, у которого он есть

**Видеостена**

- Сетка до 8×8
- У каждого слота свой фрагмент (окно разметки I/O, как в монтажке)
- Кнопка «Фрагменты по короткому»: всем роликам вход в начале файла, длина как у самого короткого
- Превью ячейки — первый кадр фрагмента, длина видна на слоте
- Длина стены — по самому короткому фрагменту; при большом разбросе — предупреждение
- Экспорт без выбранного фрагмента не запускается

Сборку можно остановить. Неполный выходной файл при отмене удаляется.

## Требования

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` и `ffprobe` в `PATH` (превью кадров, синхронизация по звуку, кодирование)

## Установка и запуск

```bash
git clone https://github.com/DarkDemiurg/video-puzzle.git
cd video-puzzle
uv sync
uv run video-puzzle
```

Интерфейс на русском.

## Разработка

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

Точка входа: `src/video_puzzle/`. GUI-тесты идут с `QT_QPA_PLATFORM=offscreen` (см. `tests/conftest.py`).

## Лицензия

[MIT](LICENSE)
