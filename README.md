# Video Puzzle

[![CI](https://github.com/DarkDemiurg/video-puzzle/actions/workflows/ci.yml/badge.svg)](https://github.com/DarkDemiurg/video-puzzle/actions/workflows/ci.yml)
[![Release](https://github.com/DarkDemiurg/video-puzzle/actions/workflows/release.yml/badge.svg)](https://github.com/DarkDemiurg/video-puzzle/releases)

**Video Puzzle** — desktop app for tiling several videos into one mosaic and exporting the result with **ffmpeg**.  
Two modes: classic **puzzle** (2–4 clips) and a **video wall** (grid up to 8×8 with per-cell fragments).

Десктопное приложение на **Python / PySide6**: склейка роликов в мозаику или видеостену, разметка фрагментов, синхронизация по звуку, экспорт в MP4. Интерфейс на русском.

## Скачать

Готовые бинарники (Linux / Windows / macOS) собираются через [PyApp](https://ofek.dev/pyapp/) и публикуются в [Releases](https://github.com/DarkDemiurg/video-puzzle/releases).

| Платформа | Архив |
|-----------|--------|
| Linux x86_64 | `video-puzzle-linux-x86_64.tar.gz` |
| Linux ARM64 | `video-puzzle-linux-aarch64.tar.gz` |
| Windows | `video-puzzle-windows-x86_64.zip` |
| macOS Apple Silicon | `video-puzzle-macos-aarch64.tar.gz` |
| macOS Intel | `video-puzzle-macos-x86_64.tar.gz` |

Распакуйте архив и запустите `video-puzzle` (или `video-puzzle.exe` на Windows).  
**ffmpeg** и **ffprobe** должны быть установлены в системе и доступны в `PATH` — бинарник приложения их не включает.

Первый запуск может занять минуту: PyApp разворачивает встроенный Python и зависимости.

## Что умеет

### Режим «Пазл» (2–4 ролика)

| Возможность | Описание |
|-------------|----------|
| Схемы | 2 горизонтально / вертикально, широкая или узкая пирамида, квадрат 2×2 |
| Общая шкала | Один момент времени во всех превью |
| Фрагмент выхода | Необязательный диапазон, например с 20 по 60 с |
| Синхронизация | Оценка сдвига по звуку в начале или в хвосте |
| Звук | Авто (первый со звуком) или выбранный слот; опционально нормализация |
| Качество | Быстрое / обычное / высокое / как оригинал |
| Кодек | Авто (NVENC → QSV → CPU) или вручную; аппаратные кодеки проверяются перед использованием |
| FPS | Из исходников (максимальный), иначе 30 |
| Зазор, кроп, поворот | Между ячейками; кроп и поворот в окне разметки |
| Кадр склейки | JPEG-превью мозаики без полного кодирования |
| Проекты `.vproj` | Сетка, файлы, фрагменты, настройки; запоминаются окно и папки |

### Режим «Видеостена»

| Возможность | Описание |
|-------------|----------|
| Сетка | До 8×8 ячеек |
| Фрагменты I/O | У каждого слота свой отрезок, как в монтажке |
| «Фрагменты по короткому» | Всем роликам вход в начале, длина как у самого короткого |
| Длина стены | По самому короткому фрагменту; предупреждение при большом разбросе |
| Экспорт | Без выбранного фрагмента сборка не запускается |

Общее: перетаскивание слотов для смены порядка, оценка размера файла перед сборкой, открытие готового файла или папки после экспорта, остановка сборки (неполный файл при отмене удаляется).

## Требования

- **ffmpeg** и **ffprobe** в `PATH` (превью, синхронизация, кодирование)
- Для запуска из исходников: Python 3.12+, [uv](https://docs.astral.sh/uv/)

## Установка из исходников

```bash
git clone https://github.com/DarkDemiurg/video-puzzle.git
cd video-puzzle
uv sync
uv run video-puzzle
```

## Сборка standalone-бинарника (PyApp)

Локально (Linux, нужны Rust и curl):

```bash
uv sync
chmod +x scripts/build-pyapp.sh
./scripts/build-pyapp.sh
# → dist/pyapp/video-puzzle
```

Публикация на GitHub: создайте тег `v0.1.0` и запушьте — workflow [release.yml](.github/workflows/release.yml) соберёт wheel, бинарники для всех платформ и создаст Release.

```bash
git tag v0.1.0
git push origin v0.1.0
```

Или запустите workflow **Release** вручную (Actions → Release → Run workflow) для тестовой сборки без тега.

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
