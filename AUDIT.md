# Аудит Video Puzzle

Дата: 2026-10-19  
Версия: 0.1.1 (рабочая копия, ветка `main`, HEAD `bf9f2f1`)  
Окружение проверки: Linux, Python 3.12.6, PySide6 6.11.2, numpy 2.5.2, ffmpeg/ffprobe 8.0.1

## 1. Что проверялось и как

- Прочитан весь код `src/video_puzzle` (≈4,8 тыс. строк): состояние, layout, probe, thumbnails, sync, project, encoders, ffmpeg_script, cache, UI.
- Прочитаны тесты (299 тестов), CI (`.github/workflows/ci.yml`, `release.yml`), скрипты сборки.
- Находки проверялись эмпирически: запуск приложения offscreen, реальные ffmpeg/ffprobe, подмена runner'ов, загрузка битых `.vproj`, закрытие окна с активными потоками, рендер с совпадающим input/output.
- Базовый прогон на момент аудита: `299 passed`, `ruff check` — чисто, `ruff format --check` — чисто.

Аудит ведётся по **рабочей копии**, а не по HEAD: в ней 22 изменённых файла и 4 untracked-файла, включая импортируемый модуль `src/video_puzzle/cache.py`. Это важно для приоритетов ниже.

> **Статус после исправлений:** все находки закрыты или смягчены; итоговый прогон —
> `348 passed`, `pytest -W error`, `ruff check`, `ruff format --check` и `mypy` без ошибок.
> Подробности по каждому пункту — в §10.

## 2. Краткое резюме

Код в целом аккуратный: нет `shell=True`, `eval`, `pickle`, командных инъекций; подпроцессы запускаются списками аргументов; тесты и линтеры зелёные. Основные проблемы — не в безопасности, а в **жизненном цикле потоков, валидации проектов и целостности пользовательских файлов**.

| Приоритет | Кол-во | Суть |
|---|---:|---|
| P0 — Critical | 2 | Аппаратные энкодеры не детектируются вообще; превью в слотах не отображаются вообще |
| P1 — High | 8 | Падение/аварийное завершение при закрытии; потеря несохранённых правок; потеря исходника при совпадении output/input; неверный экспорт из-за меток; «мёртвая» кнопка авто-фрагментов; рассинхрон сетки; небезопасная загрузка проекта; untracked-модуль |
| P2 — Medium | 17 | Некорректные ошибки probe/thumbnail; audio-only файлы; завышенная длительность; ограничения sync; гонки кэша; отсутствие таймаутов; пробелы CI и supply-chain |
| P3 — Low | 8 | Гигиена, косметика, устойчивость к редким состояниям |

---

## 3. P0 — Critical

### C1. Детект аппаратных энкодеров всегда падает с `TypeError`

**Где:** `src/video_puzzle/ui/main_window.py:154-196` (`EncoderProbeWorker`, `_runner`), `src/video_puzzle/encoders.py:60,71-79`.

`EncoderProbeWorker._runner` пытается вызывать `subprocess.Popen(cmd, **kwargs)`, а `detect_encoders`/`probe_encoder` передают аргументы `subprocess.run`: `capture_output=True`, `text=True`, `timeout=...`. `Popen` их не принимает.

Воспроизведение:

```text
detect_encoders(runner=<тот же _runner>) ->
TypeError: Popen.__init__() got an unexpected keyword argument 'capture_output'
```

`EncoderProbeWorker.run()` ловит только `_ProbeInterrupted`, поэтому `TypeError` уходит в `QThread.run()` как необработанное исключение:

```text
Error calling Python override of QThread::run(): ...
```

**Влияние:** детект не завершается никогда. Кнопки NVENC/QSV навсегда остаются disabled с подсказкой «Проверяем…», `_encoders_detected` остаётся `False`, приложение молча работает только на CPU. В тестах это не видно, потому что `tests/conftest.py:26-34` подменяет `_start_encoder_probe` на no-op, а `tests/test_encoders.py` подаёт фейковый runner, совместимый с `subprocess.run`.

**Исправление:**
- В `_runner` явно разобрать контракт `run`: `timeout`, `check`, `capture_output`; запускать `Popen(stdout=PIPE, stderr=PIPE, text=True)` и опрашивать `communicate(timeout=0.2)`.
- Уважать `timeout` (сейчас он бы тоже попал в `Popen` и дал `TypeError`).
- В `run()` ловить `Exception`, эмитить `failed(str)` и в UI показывать «не удалось проверить, остался CPU», а не вечное «Проверяем…».
- Добавить регрессионный тест, который запускает **настоящий** `EncoderProbeWorker` с реальным `ffmpeg -encoders` (или с runner'ом, повторяющим сигнатуру `subprocess.run`, а не `Popen`).

### C2. Превью в слотах не отображаются: `QPixmap` передаётся как путь

**Где:** `src/video_puzzle/ui/main_window.py:1332-1337`, `src/video_puzzle/ui/slot_widget.py:116-117`.

```python
# main_window.py
self.canvas.slots[index].set_thumbnail(Path(video), QPixmap(dest))

# slot_widget.py
def set_thumbnail(self, path: Path, image: Path) -> None:
    self._pixmap = QPixmap(str(image))
```

`str(QPixmap)` даёт `"<PySide6.QtGui.QPixmap(...) at 0x...>"`, а не путь. `QPixmap` из такой строки — null. `_scale_preview()` при null pixmap делает ранний `return`, поэтому в слоте навсегда остаётся текст «Извлекаю кадр…».

Воспроизведение (offscreen, реальный JPEG):

```text
preview text after thumb: 'Извлекаю кадр…'
pixmap null: True
```

**Влияние:** ключевая визуальная функция — превью роликов — не работает. Пользователь видит пустые/вечно загружающиеся ячейки. Тест `test_current_thumbnail_is_shown` (`tests/test_main_window_workers.py:368-381`) проверяет только `filename.text()`, поэтому баг зелёный.

**Исправление:**
- Либо `set_thumbnail(path: Path, image: QPixmap)` и `self._pixmap = image`, либо передавать `dest` (путь) и оставить сигнатуру. Первый вариант честнее: pixmap уже загружен в GUI-потоке.
- В тесте проверять `not widget.preview.pixmap().isNull()` на реальном JPEG.
- Добавить в CI проверку типов (mypy/pyright): этот баг — ровно то, что ловит статическая типизация.

---

## 4. P1 — High

### H1. Закрытие окна уничтожает работающие `QThread` → аварийное завершение

**Где:** `src/video_puzzle/ui/main_window.py:1709-1730`, воркеры `71-216`, `1262-1270`, `1308-1320`, `1345-1361`.

`closeEvent` делает `wait(200)` для `_workers` и `_still_worker`, но не отменяет их. `ThumbnailWorker`/`ProbeWorker`/`SyncWorker`/`StillWorker` не имеют `cancel()`; `ThumbnailWorker` может ждать ffmpeg до 20 с, `StillWorker` вызывает `subprocess.run` вообще без таймаута. После `wait(200)` окно и список `_workers` уничтожаются, поток остаётся running:

```text
QThread: Destroyed while thread '' is still running
```

Для `EncoderProbeWorker` ожидание 3 с, но результат `wait()` не проверяется: если поток не успел, `_encoder_probe = None` снимает последнюю ссылку — тот же abort. Воспроизведено с медленным runner'ом:

```text
close returned after 3.0 s; probe: None
QThread: Destroyed while thread '' is still running
```

**Влияние:** приложение может аварийно завершиться при закрытии; на медленном диске/сети/GPU-стеке — стабильно. Это ещё и потеря незавершённых операций.

**Исправление:**
- Ввести общий протокол `cancel()` для всех воркеров: флаг + `requestInterruption()` + убийство текущего `Popen`/`QProcess` (для probe/thumbnail/still).
- В `closeEvent`: `_closing = True`; вызвать `cancel()` у всех; ждать с дедлайном; если поток не завершился — `terminate()` + `wait()` как последнее средство; **не обнулять ссылку**, пока `isRunning()`.
- `StillWorker`: заменить `subprocess.run` на тот же отменяемый runner с таймаутом.
- `EncoderProbeWorker.cancel()` должен убивать `self._process`, а `_process` инициализировать в `__init__`.
- Тест: подменить thumbnail-runner на «спящий», закрыть окно, проверить отсутствие предупреждения/abort и что процесс завершился.

### H2. Кнопка «Фрагменты по короткому» disabled ровно тогда, когда нужна

**Где:** `src/video_puzzle/ui/main_window.py:1395`.

```python
self.auto_frag_btn.setEnabled(self.state.is_wall and ready and probed)
```

`ready = self.state.is_ready_for_export()`, а для стены `is_ready_for_export()` требует, чтобы **у всех ячеек уже были метки I/O**. То есть кнопка, которая эти метки и расставляет, включается только после того, как метки уже стоят.

Воспроизведение:

```text
wall ready: False auto_frag enabled: False
# прямой вызов _auto_wall_fragments() работает:
after auto: True auto_frag enabled: True
```

Тест `tests/test_preview_canvas.py:63-64` проверяет только пустую сетку (там disabled корректно), а случая «файлы есть, меток нет» в тестах нет — поэтому регрессии не видно.

**Исправление:**

```python
self.auto_frag_btn.setEnabled(
    self.state.is_wall and self.state.is_complete() and probed and not running
)
```

Добавить тест: стена 2×2 заполнена, меток нет → кнопка enabled; нажатие реально ставит метки.

### H3. Закрытие окна молча теряет несохранённые изменения

**Где:** `src/video_puzzle/ui/main_window.py:1709-1730`; ср. `_confirm_discarding()` на `1101-1111`, который используется только при открытии другого проекта (`1142`).

`closeEvent` сохраняет геометрию/настройки, убивает рендер и потоки, но **не проверяет `_is_dirty()`**. Пользователь, расставивший ролики и не нажавший «Сохранить проект», теряет работу без предупреждения.

**Исправление:** в `closeEvent` при `_is_dirty()` показать `Save / Discard / Cancel`:
- `Cancel` → `event.ignore()`;
- `Save` → вызвать сохранение (с диалогом пути), при отказе/ошибке не закрывать;
- `Discard` → закрывать.
Учесть, что `closeEvent` вызывается и программно в тестах: в тестах либо заранее `_mark_saved()`, либо подменить диалог.

### H4. `load_project` не валидирует проект и пропускает опасные значения

**Где:** `src/video_puzzle/project.py:37-45, 85-133`.

Подтверждённые случаи:

| Вход | Результат |
|---|---|
| `"trim_start": "oops"` | `ValueError: could not convert string to float` (не `ProjectError`) |
| `"rotation": "x"` | `ValueError: invalid literal for int()` |
| `"crop": ["a","b","c","d"]` | `ValueError` |
| `"slots": "nope"` | тихо загружается 0 слотов, без ошибки |
| `"version": 99` | принимается без предупреждения |
| `"trim_start": Infinity` | принимается |
| `"crop": [NaN, NaN, NaN, NaN]` | принимается, затем ffmpeg получает `crop=iw*nan:...` |
| `"path": "../secret.mp4"` | принимается; путь не нормализуется |
| несуществующий файл | принимается; `is_complete() == True` |
| `"include_audio": "false"` | `bool("false") == True` |

Дополнительно: слоты за пределами активной сетки загружаются в состояние (до `MAX_SLOTS`), что позже даёт рассинхрон с UI (см. H7). `_open_project` ловит только `ProjectError` (`main_window.py:1153-1157`), поэтому «сырой» `ValueError` вылетает из слота Qt как traceback.

**Влияние:** битый/чужой `.vproj` может уронить открытие, включить экспорт с несуществующими файлами, отправить ffmpeg в путь вне папки проекта, получить NaN/Inf в фильтрах. Это не RCE (ffmpeg запускается списком аргументов), но это отказ в обслуживании и неверный экспорт.

**Исправление:**
- Обернуть разбор каждого слота в `try/except (TypeError, ValueError, OverflowError)` → `ProjectError(f"Слот {index+1}: ...")`.
- Проверять `version`: `> PROJECT_VERSION` — отказ с понятным сообщением; `<=` — загружать.
- Требовать `slots` — список (или отсутствие).
- Для чисел: `math.isfinite`, диапазоны (`trim_start >= 0`, `0 <= rotation < 360`, кроп в `[0,1]` и `width/height > 0`).
- `_resolve_path`: `Path(raw).expanduser(); path.resolve()`; не запрещать абсолютные пути (это законная функция), но нормализовать `..`.
- Проверять существование файла; отсутствующие — не добавлять в слот и показать предупреждение после загрузки (или отдельный `missing_files` в результате).
- `bool` брать только если значение действительно `bool`, иначе ошибка/дефолт.
- Добавить параметризованные тесты на все строки таблицы.

### H5. Метки за пределами длительности не клампятся → пустой/неверный экспорт

**Где:** `src/video_puzzle/project.py:124-128`, `src/video_puzzle/state.py:56-61,217-225`, `main_window.py:_on_probe_ok`.

При загрузке проекта длительность ещё неизвестна (`duration=None`), поэтому `Slot.set_marks` не может клампнуть. После асинхронного probe длительность появляется, но метки не пересчитываются:

```text
клип 10 с, mark_in=20, mark_out=30
→ mark_in=20.0, mark_out=30.0, fragment_duration=10.0, has_fragment=True
→ is_ready_for_export() == True
→ source_window() == (20.0, 30.0)
```

ffmpeg получает `-ss 20 -t 10` на 10-секундном файле: кадров нет, выход пустой/ошибочный, но UI считает стену готовой.

**Исправление:** после `set_probe` вызывать `slot.clamp_marks_to_duration()`:
- `mark_in = min(max(0, mark_in), duration - 0.04)`;
- `mark_out = min(max(mark_in + 0.04, mark_out), duration)`;
- если исходный `mark_in >= duration - 0.04` — считать метки невалидными и очищать (иначе получится фрагмент 0.04 с);
- в UI показывать «метки выходят за длительность, разметьте заново».
Тест: загрузить стену с метками 20–30, подать probe 10 с, проверить, что `has_fragment` false (или метки в пределах) и экспорт недоступен.

### H6. Output может совпасть с input и уничтожить исходник

**Где:** `src/video_puzzle/ui/main_window.py:1505-1520` (`_pick_output`), `1554-1610` (`_render`), `1522-1552` (`_save_script`).

Проверки «output ≠ input» нет. При точном совпадении строк ffmpeg сам отказывается (`Output ... same as Input #0`, код 234). Но при другом написании пути — symlink, hardlink, `./file.mp4`, `sub/../file.mp4` — ffmpeg **не** распознаёт совпадение и перезаписывает файл:

```text
$ ln -s src_sym.mp4 out_sym.mp4
$ ffmpeg -y -i src_sym.mp4 -c:v libx264 out_sym.mp4
exit=0
$ ls -l src_sym.mp4 out_sym.mp4
out_sym.mp4 -> src_sym.mp4
-rw-r--r-- src_sym.mp4   # файл перекодирован поверх себя, исходник потерян
```

Аналогично для `./rel.mp4` и hardlink. `_render_preexisting = output.exists()` в этом случае `True`, поэтому приложение даже не удалит «частичный» файл — но исходник уже перезаписан.

**Влияние:** потеря исходного видео пользователя. Это самый опасный дефект после P0.

**Исправление:**
- Перед запуском сравнивать output со всеми активными входами по идентичности файла:
  ```python
  def same_file(a: Path, b: Path) -> bool:
      try:
          if a.exists() and b.exists():
              return os.path.samefile(a, b)
      except OSError:
          pass
      return a.resolve() == b.resolve()
  ```
- Если совпало — предупреждение и возврат к выбору пути; не запускать ffmpeg.
- То же для `_save_script` (скрипт хотя бы не должен рекламировать затирание исходника).
- Тест: output — symlink/hardlink/relative-путь к input; проверить, что рендер не стартует и исходник цел.

### H7. Изменение сетки стены рассинхронизирует состояние и виджеты

**Где:** `main_window.py:1023-1093` (`_apply_state_to_widgets`), `666-675` (`_apply_canvas_layout`), `688-693` (`_on_wall_grid`), `preview_canvas.py:91-116`.

`_apply_state_to_widgets` для `index >= count` вызывает `widget.set_empty()`, но **не очищает `state.slots[index]`**. При росте сетки обратно виджеты не восстанавливаются. Воспроизведение 3×3 → 2×2 (с применением состояния) → 3×3:

```text
state path: g8.mp4 | widget filled: False | filename: '' | preview: 'Перетащите видео...'
state ready: True | render enabled: True
```

То есть состояние считает ячейку заполненной и стену готовой, а на экране пустая ячейка. Пользователь видит «Готово» и может запустить экспорт, не видя часть исходников.

**Исправление:** при изменении сетки синхронизировать **каждую активную** ячейку из состояния: если `slot.path` есть, а виджет пуст — `set_loading` + `_probe_slot` + `_request_thumbnail`; если пути нет — `set_empty`. Не вызывать `set_empty` для скрытых ячеек, если состояние их сохраняет, либо очищать и состояние. Добавить тест 3×3→2×2→3×3.

### H8. `cache.py` импортируется, но не находится под контролем версий

**Где:** `src/video_puzzle/ui/main_window.py:35`, untracked `src/video_puzzle/cache.py`; также untracked `tests/test_cache.py`, `tests/test_encoders.py`, `tests/test_main_window_workers.py`.

`main_window` импортирует `clear_cache, prune_cache`. Если собрать wheel/архив из tracked-файлов (или сделать `git commit -a` без `git add`), приложение упадёт на импорте. Это не «баг кода», а состояние репозитория, но для релиза оно критично.

**Исправление:** `git add src/video_puzzle/cache.py tests/test_cache.py tests/test_encoders.py tests/test_main_window_workers.py`, удалить/переместить `_score_probe.py` (untracked отладочный скрипт в корне).

---

## 5. P2 — Medium

### M1. `OSError` и `JSONDecodeError` не оборачиваются в `ProbeError`/`ThumbnailError`

**Где:** `probe.py:57-58, 112-118`; `thumbnails.py:56-73`; воркеры `main_window.py:112-125, 92-100`.

`probe_video(..., runner=boom)` с `FileNotFoundError` пробрасывает `FileNotFoundError`, а `ProbeWorker` ловит только `ProbeError`. `parse_probe_json("not json")` даёт `JSONDecodeError`. `extract_thumbnail` с `OSError` — тоже не `ThumbnailError`. В результате поток печатает traceback и **не эмитит** `failed`; слот остаётся в «Извлекаю кадр…», кнопки не обновляются.

**Исправление:** обернуть `except OSError` в `ProbeError`/`ThumbnailError`; `parse_probe_json` — `except (json.JSONDecodeError, AttributeError, TypeError)`. В воркерах дополнительно ловить `Exception` и эмитить `failed`, чтобы UI никогда не зависал.

### M2. Audio-only `.mp4` принимается как видео

**Где:** `state.py:17-18` (`is_video_file` — только расширение), `probe.py:57-88` (нет `has_video`), `thumbnails.py`.

Проверено на реальном файле: `audioonly.mp4` (только AAC) проходит `is_video_file`, `probe_video` возвращает `fps=None`, `extract_thumbnail` падает с `ThumbnailError: Output file does not contain any stream`. Экспорт затем упадёт на фильтре `[0:v]`.

**Исправление:** добавить `has_video: bool` в `ProbeResult` и `Slot`; заполнять по наличию `codec_type == "video"`; при добавлении файла без видеопотока показывать понятную ошибку и не считать слот готовым. Тест с реальным audio-only файлом (skip, если нет ffmpeg).

### M3. Длительность завышается из-за аудиопотока

**Где:** `probe.py:65-86`.

`parse_probe_json` берёт `max(stream.duration, format.duration)`. Для файла «видео 10 с + аудио 12 с» результат — 12 с:

```text
probe: ProbeResult(duration=12.0, has_audio=True, fps=30.0)
stream durations: video 10.0, audio 12.0
```

Это искажает overlap, trim, timeline, прогресс и оценку размера; ffmpeg при `-shortest` даст 10 с.

**Исправление:** приоритет — максимальная длительность **видеопотока**; `format.duration` использовать только если видеопотоков нет/у них нет длительности; аудио не должно удлинять ролик.

### M4. Готовность к экспорту не требует успешного probe

**Где:** `state.py:273-277`; `main_window.py:1383-1395`; `is_complete()` проверяет только `path is not None`.

После выбора файлов, но **до** probe, `render_btn` уже enabled, статус пишет «Готово … 30.00 fps · звук: нет файлов со звуком». Если probe упал (нет файла, битый файл, audio-only), кнопка остаётся enabled, а ошибка видна только в углу слота.

**Исправление:** для `save/render/preview` требовать `all(slot.duration is not None)` и отсутствие `probe_error`; добавить в `Slot` состояние ошибки; при ошибке показывать её в статусе и блокировать экспорт.

### M5. Ограничения синхронизации по звуку

**Где:** `sync.py:20` (`MIN_SCORE = 0.3`), `162-238` (`align_slots`), `78-116` (`lag_seconds`), `241-257` (`analyze_slots`), `254-274` (`apply_sync`).

- Порог 0.3 довольно низкий; score — нормированная корреляция по всей длине, без нормировки на перекрытие.
- Нет проверки «остроты» пика: периодический сигнал (тон, музыка с общим ритмом) может дать высокий score на кратном периоде. Инверсия фазы даёт score ≈ 1.0 на лаге полупериода.
- Все клипы сравниваются только с первым аудиоклипом; ошибка в нём портит все лаги.
- `analyze_slots` молча превращает `SyncError` в `None`, то есть «нет звука», и пользователь не узнаёт, что файл не прочитался.

Важно: демонстрация «два независимых музыкальных клипа → 0.5688» из `_score_probe.py` **некорректна**: оба сигнала в этом скрипте имеют одинаковую основную частоту 220 Гц и одинаковую 2 Гц амплитудную огибающую, поэтому они не независимы. На действительно независимых синтетических сигналах score ≈ 0.004–0.03. Проблема реальна для периодических/общих ритмических структур, но её нельзя описывать как «любые два клипа дают ложное срабатывание».

**Исправление:** поднять порог до 0.5–0.6; считать confidence как отношение пика к второму пику вне окна ±0.25 с; нормировать корреляцию на длину перекрытия; при низком confidence явно писать «сдвиг не определён» и не применять; сравнивать каждый клип с несколькими опорными (медиана лагов); показывать ошибки извлечения аудио.

### M6. `_on_files_dropped` падает на пустом списке

**Где:** `main_window.py:1225`.

```python
first, *rest = paths   # ValueError: not enough values to unpack
```

Из UI пустой список не приходит (`dropEvent` фильтрует), но сигнал публичный; вызов из теста/будущего кода роняет слот. **Исправление:** `if not paths: return`.

### M7. TrimEditor открывается до завершения probe

**Где:** `main_window.py:695-707`, `trim_editor.py:153-170`.

Если `slot.duration is None`, `TrimEditor` берёт `duration=0`, `default_out=1.0`, и метки ограничиваются 1 секундой. Пользователь, открывший I/O сразу после drop, получает неверный диапазон; после probe он не пересчитывается.

**Исправление:** не открывать редактор до `duration is not None` (показать «Дождитесь определения длительности») либо пересчитывать `_duration`/клампы по сигналу probe.

### M8. `save_project` пишет файл неатомарно

**Где:** `project.py:67-74` (`write_text` напрямую).

Падение/зависание в момент записи оставляет обрезанный `.vproj`. **Исправление:** писать во временный файл в той же папке, `flush`+`fsync`, затем `os.replace(tmp, path)`.

### M9. Кэш общий для всех экземпляров приложения

**Где:** `main_window.py:266-279` (`clear_cache` при старте), `cache.py:41-69`, имена `slot-{index}-{gen}.jpg`, `mosaic-preview.jpg`.

Второй запущенный экземпляр на старте удаляет кадры первого; одинаковые имена (`mosaic-preview.jpg`) перезаписываются. Превью могут пропадать/мигать.

**Исправление:** подпапка на процесс (`.../video-puzzle/<pid>-<uuid>/`) или `QLockFile`; `clear_cache` — только своей папки; чистить чужие/старые по mtime.

### M10. Жёстко зашитый `"ffmpeg"` в аргументах

**Где:** `ffmpeg_script.py:218, 237`.

Probe/thumbnails ищут бинарник через `shutil.which`, а рендер/кадр склейки передают в `QProcess` строку `"ffmpeg"`. Если PATH у GUI-приложения отличается (типично для macOS/Windows), `QProcess` не стартует, а сообщение будет общим «Не удалось запустить ffmpeg».

**Исправление:** передавать в `build_ffmpeg_args`/`build_still_args` разрешённый путь (или резолвить в `_render`), сохранив `"ffmpeg"` только в shell-скрипте для переносимости; при старте проверять наличие ffmpeg/ffprobe и показывать внятную ошибку.

### M11. `StillWorker` без таймаута и с возможным «устаревшим» успехом

**Где:** `main_window.py:127-147`, `ffmpeg_script.py:235-252`.

`subprocess.run` без `timeout`; зависший ffmpeg держит поток и мешает закрытию. `dest` не удаляется перед запуском, поэтому если ffmpeg вернул 0, но ничего не записал, а старый `mosaic-preview.jpg` существует — покажется старая картинка.

**Исправление:** таймаут + отмена; `dest.unlink(missing_ok=True)` перед запуском; проверять mtime/размер.

### M12. CI и тесты не ловят найденные классы дефектов

**Где:** `.github/workflows/ci.yml`, `tests/conftest.py:26-34`, `tests/test_preview_canvas.py:63-64`, `tests/test_main_window_workers.py:368-381`.

- Нет проверки типов — C2 (Path vs QPixmap) не ловится.
- `conftest` отключает реальный `_start_encoder_probe` — C1 не ловится.
- Нет теста «стена заполнена, меток нет» для авто-фрагментов (есть только пустая сетка).
- Тест превью проверяет только имя файла, не pixmap.
- Нет тестов на закрытие окна с активными воркерами, на symlink/hardlink output, на битые проекты.
- Тесты идут только на Ubuntu, хотя релизы собираются для Windows/macOS.

**Исправление:** добавить mypy/pyright (с учётом PySide6 stubs), `pytest -W error`, тесты на воркеры/закрытие/пути, матрицу ОС для тестов, покрытие хотя бы для `project`, `state`, `ffmpeg_script`, `sync`.

### M13. Supply-chain риски в сборке

**Где:** `scripts/build-pyapp.sh:27`, `scripts/prepare-pyapp-distribution.sh:21`, `.github/workflows/release.yml:105,124`.

PyApp и python-build-standalone скачиваются `curl` без проверки контрольной суммы/подписи; `cargo build --release` без `--locked`; `pip install` без хешей. Компрометация релиза/зеркала или подмена зависимости даёт исполняемый код в бинарнике.

**Исправление:** зафиксировать SHA-256 для архивов и проверять `sha256sum -c`; `cargo build --locked`; `pip install --require-hashes` (или ставить из локального lock-файла); публиковать checksums/SBOM к релизу.

### M14. `uv.lock` был рассинхронизирован с `pyproject.toml`

**Где:** `uv.lock`.

На момент аудита `uv lock --check` падал: в lock отсутствовали dev-зависимости (`pytest`, `ruff`, `pluggy`, ...), а `uv sync --frozen --all-groups` не устанавливал их. `uv run ruff` пересобрал lock и поставил ruff 0.17.1 вместо зафиксированного 0.16.5 — то есть CI получал невоспроизводимый тулинг.

**Статус:** в ходе аудита `uv run` пересобрал `uv.lock`; сейчас `uv lock --check` проходит. Это единственное изменение отслеживаемого файла, которое аудит внёс в рабочую копию (плюс сам `AUDIT.md`), — проверьте diff перед коммитом. Рекомендация: в CI добавить `uv lock --check` отдельным шагом, чтобы такое не проходило.

### M15. `parse_probe_json` падает на не-словаре/неверных типах

**Где:** `probe.py:57-88`.

`json.loads("[]")` → `AttributeError: 'list' object has no attribute 'get'`; `{"streams": "x"}` → `AttributeError` на `stream.get`. Это не `ProbeError`, поэтому воркер снова зависает (см. M1).

**Исправление:** проверять `isinstance(data, dict)`, `isinstance(streams, list)`, `isinstance(stream, dict)`; иначе `ProbeError`.

### M16. Приведение типов через `bool()` в проекте

**Где:** `project.py:100,109,111`.

`bool("false") is True`; `bool(0) is False`. JSON обычно даёт настоящие bool, но повреждённый/чужой проект — нет. **Исправление:** принимать только `bool`, иначе ошибка или дефолт.

### M17. `_save_script` молча перезаписывает существующий `.sh`

**Где:** `main_window.py:1522-1552`.

`script_path.write_text(...)` без проверки существования. Пользователь может потерять свой скрипт. **Исправление:** спрашивать подтверждение или писать `output.with_suffix(".sh")` только после проверки.

---

## 6. P3 — Low / гигиена

1. **`_score_probe.py`** — untracked отладочный скрипт в корне с генерацией сигналов и `print`. Удалить или перенести в `scripts/diagnostics/` с тестами.
2. **Зазор в превью vs экспорт**: `grid_output_size` добавляет gap к каждой ячейке, включая правую/нижнюю границу; превью рисует внешние поля 12 px и gap только между ячейками. Косметическое расхождение.
3. **`_on_probe_ok` меняет playhead** (`main_window.py:1282-1305`): при `playhead == 0` и overlap > 1 ставит 1 с. Это неожиданный побочный эффект probe; лучше не трогать позицию пользователя.
4. **`closeEvent` блокирует GUI** до 3 с (probe) / 15 с (trim editor) / 200 мс на воркер. Нужен неблокирующий сценарий с прогрессом/отменой.
5. **Нет логирования и глобального exception hook**: ошибки в слотах Qt печатаются в stderr и теряются; для десктопа нужен лог-файл.
6. **Fallback кэша `"/tmp"`** (`main_window.py:269-271`) на Windows некорректен; лучше `tempfile.gettempdir()`.
7. **`set_marks`/`set_probe` и inf/NaN**: `set_probe` с `inf` даёт бесконечную длительность; добавить `math.isfinite`.
8. **`_render` kill + `waitForFinished(500)`**: если ffmpeg не умер за 500 мс, `QProcess` уничтожается вместе с окном; увеличить/логировать, убивать группу процессов.

---

## 7. Безопасность: что проверено и в порядке

- **Нет командных инъекций**: `subprocess.run/Popen` и `QProcess` получают списки аргументов; `shell=True` не используется; `render_shell_script` собирает команду через `shlex.join`.
- **Нет `eval`/`exec`/`pickle`/`yaml.load`** и подобного.
- **Пути**: проект может ссылаться на абсолютные файлы — это заявленная функция (медиа может лежать вне папки проекта), поэтому это не «path traversal» в смысле выхода за границу доверия. Но нормализация `..` и проверка существования нужны (H4).
- **Основные реальные риски**: H6 (потеря исходника), H4 (битый проект/NaN/Inf), M13 (supply-chain), M9 (общий кэш).

---

## 8. План исправления

### Этап 0 — «чтобы работало» (0,5–1 день)

1. **C1**: починить `EncoderProbeWorker._runner` (Popen + timeout + cancellation), добавить `failed`-сигнал и fallback на CPU; тест с настоящим runner'ом.
2. **C2**: `SlotWidget.set_thumbnail` принимает `QPixmap` (или `main_window` передаёт путь); тест на не-null pixmap.
3. **H8**: `git add` для `cache.py` и трёх тестов; убрать `_score_probe.py`.
4. **M14**: зафиксировать `uv.lock`; добавить `uv lock --check` в CI.

### Этап 1 — «не терять данные и не падать» (1–2 дня)

5. **H1**: единый отменяемый runner для probe/thumbnail/still; `cancel()` у всех воркеров; корректный `closeEvent` (cancel → wait с дедлайном → terminate).
6. **H3**: диалог Save/Discard/Cancel при закрытии с несохранёнными изменениями.
7. **H6**: проверка идентичности output и всех input (`samefile` + `resolve`), запрет запуска; тесты symlink/hardlink/relative.
8. **H4**: полная валидация `load_project` (типы, версия, конечные числа, `slots`-список, существование файлов, нормализация путей); `_open_project` ловит всё и показывает ошибку.
9. **H5**: кламп/очистка меток после probe; тест «метки 20–30 на 10-секундном клипе».

### Этап 2 — «правильные состояния и экспорт» (2–3 дня)

10. **H2**: условие доступности `auto_frag_btn`; добавить тест «стена заполнена, меток нет».
11. **H7**: синхронизация виджетов при росте сетки; тест 3×3→2×2→3×3.
12. **M1/M15**: обернуть `OSError`/`JSONDecodeError`/`AttributeError` в `ProbeError`/`ThumbnailError`; воркеры ловят `Exception` и эмитят `failed`.
13. **M2/M3/M4**: `has_video`, приоритет видеодлительности, блокировка экспорта без успешного probe.
14. **M5**: поднять порог sync, добавить confidence/второй пик, не применять сомнительный сдвиг.
15. **M6/M7**: пустой drop; запрет TrimEditor до probe.

### Этап 3 — «устойчивость и воспроизводимость» (2–4 дня)

16. **M8**: атомарное сохранение проекта.
17. **M9**: кэш на процесс/инстанс.
18. **M10/M11**: resolved ffmpeg path, таймаут StillWorker, очистка dest.
19. **M12**: mypy/pyright, `pytest -W error`, матрица ОС, тесты на воркеры/закрытие/проекты/пути, покрытие ключевых модулей.
20. **M13**: checksums для PyApp/python-build-standalone, `cargo build --locked`, `pip --require-hashes`.
21. **M16/M17**: строгий bool, подтверждение перезаписи `.sh`.

### Этап 4 — Low

22. Логирование/exception hook, `tempfile.gettempdir()`, `isfinite` в `set_probe`, косметика зазора, playhead, закрытие.

### Регрессионные тесты, которые обязательно добавить

| Находка | Тест |
|---|---|
| C1 | `EncoderProbeWorker` с runner'ом, имитирующим `subprocess.run`; проверить, что сигнал `succeeded` приходит и HW-энкодеры доступны |
| C2 | реальный JPEG → `_on_thumb_ok` → `preview.pixmap().isNull() == False` |
| H1 | медленный thumbnail-runner → `close()` → нет `QThread: Destroyed...`, процесс завершён |
| H2 | стена 2×2 без меток → кнопка enabled → клик → метки стоят |
| H3 | грязное состояние → close → диалог; Cancel не закрывает |
| H4 | параметризованно: `trim_start:"oops"`, `rotation:"x"`, `crop:["a"...]`, `slots:"nope"`, `version:99`, `inf`, `nan`, `../`, missing file |
| H5 | метки 20–30, probe 10 с → не ready/метки в пределах |
| H6 | output = symlink/hardlink/`./`-путь к input → рендер не стартует, файл цел |
| H7 | 3×3→2×2→3×3 → виджеты и `is_ready_for_export()` согласованы |
| M2 | audio-only `.mp4` → понятная ошибка, слот не ready |
| M3 | JSON video 10/audio 12/format 12 → duration 10 |
| M5 | независимые сигналы → score ниже порога; одинаковый сигнал со сдвигом → верный лаг |
| M6 | `_on_files_dropped(0, [])` → без исключения |

---

## 9. Приоритетный вывод

Если делать только три вещи — это **C1, C2, H1**: без них приложение либо не показывает превью, либо не использует аппаратные кодеки, либо может аварийно закрыться. Сразу за ними — **H3 и H6**: это прямые потери данных пользователя (несохранённый проект и перезаписанный исходник). Валидация проекта (**H4/H5**) — следующий обязательный шаг, потому что от неё зависит корректность экспорта.

---

## Приложение: команды воспроизведения

```bash
# C1
.venv/bin/python - <<'PY'
import subprocess
from video_puzzle.encoders import detect_encoders
def runner(cmd, **kw):
    kw.pop("check", None)
    p = subprocess.Popen(cmd, **kw)
    return subprocess.CompletedProcess(cmd, p.wait())
detect_encoders(runner=runner)
PY

# C2 (offscreen)
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap
app = QApplication([])
pm = QPixmap("/tmp/nonexistent.jpg")
print(repr(str(pm)), QPixmap(str(pm)).isNull())
PY

# H1 (медленный thumbnail)
QT_QPA_PLATFORM=offscreen .venv/bin/python /tmp/opencode/verify11.py

# H5/H6
ffmpeg -y -i src.mp4 -c:v libx264 out.mp4   # out.mp4 — symlink/hardlink на src.mp4
```

Полные проверочные скрипты аудита лежат в `/tmp/opencode/verify*.py` (вне репозитория).

---

## 10. Статус исправлений

Исправления внесены в рабочую копию (без коммита). Проверки на итоговом состоянии:

```text
361 passed in 53.12s          # pytest -W error
ruff check src tests           # All checks passed
ruff format --check src tests  # 45 files already formatted
mypy src/video_puzzle          # Success: no issues found in 26 source files
uv lock --check                # lockfile is in sync
```

| Пункт | Статус | Что сделано / где смотреть |
|---|---|---|
| C1 | ✅ | `_CancelableWorker._run` реализует контракт `subprocess.run` (`check`, `capture_output`, `text`, `timeout`), опрашивает `communicate(0.1)` и убивает процесс при отмене. `EncoderProbeWorker` ловит `Exception` и эмитит `failed`. Тест: `test_encoder_worker_honours_the_subprocess_run_contract`. |
| C2 | ✅ | `SlotWidget.set_thumbnail(Path, QPixmap)`; `_on_thumb_ok` передаёт уже загруженный pixmap. Тест проверяет не-null `preview.pixmap()` на реальном JPEG. |
| H1 | ✅ | Все воркеры наследуют `_CancelableWorker`; `closeEvent` вызывает `cancel()`, ждёт с общим дедлайном 3 с, затем `terminate()`. `StillWorker` переведён на отменяемый runner с таймаутом. Тест с «спящим» ffmpeg. |
| H2 | ✅ | `auto_frag_btn` включается при `is_wall and is_complete and probed and not running`, а не при полной готовности. Тест: заполненная стена без меток. |
| H3 | ✅ | При закрытии видимого грязного окна показывается Save / Discard / Cancel; Cancel оставляет окно открытым. Программные закрытия и тесты диалог не блокируют. |
| H4 | ✅ | `load_project` валидирует типы, версию, конечность чисел, диапазоны, `crop`, `slots`, bool; пути нормализуются `resolve()`, отсутствующие файлы пропускаются и возвращаются в `missing`. Параметризованные тесты на все случаи. |
| H5 | ✅ | `Slot.clamp_marks_to_duration()` вызывается из `set_probe`; метки за концом очищаются, частично выходящие — клампятся. Тест «20–30 с на 10-секундном клипе». |
| H6 | ✅ | `_same_file` использует `os.path.samefile` и `resolve()`; `_render`/`_save_script` отказываются писать поверх входа, включая symlink и hardlink. Тесты. |
| H7 | ✅ | `_restore_widget_cells()` возвращает скрытым ячейкам файл/превью при росте сетки и смене схемы. Тест 3×3 → 2×2 → 3×3. |
| H8 | ✅ | `cache.py` и три тестовых файла добавлены в индекс git; `_score_probe.py` удалён. |
| M1/M15 | ✅ | `OSError`, `JSONDecodeError`, неверные JSON-типы оборачиваются в `ProbeError`/`ThumbnailError`; воркеры ловят `Exception` и всегда эмитят `failed`. |
| M2 | ✅ | `ProbeResult.has_video` и `Slot.has_video`; audio-only файл блокирует экспорт с понятной ошибкой. |
| M3 | ✅ | Приоритет длительности видеопотока; аудио и `format.duration` не удлиняют ролик. Тест video 10 / audio 12 / format 12 → 10. |
| M4 | ✅ | `is_ready_for_export()` требует успешный probe (`duration`, нет `probe_error`, `has_video`) для всех активных слотов; UI блокирует экспорт и показывает статус. |
| M5 | ✅ | `MIN_SCORE=0.5`, `MIN_CONFIDENCE=2.0`, проверка второго пика, медиана по нескольким опорным клипам, `audio_errors` в результате и сообщении. Тесты на тон, независимый шум и pairwise-медиану. |
| M6 | ✅ | `_on_files_dropped` игнорирует пустой список. Тест. |
| M7 | ✅ | `TrimEditor` не открывается до `duration`; показывается сообщение. Тест. |
| M8 | ✅ | `save_project` пишет во временный файл в той же папке, `flush`+`fsync`, затем `os.replace`. |
| M9 | ✅ | Кэш в подпапке процесса (`pid-uuid`), `prune_cache_dirs` чистит только старые чужие папки; свой кэш очищается на старте. Тест. |
| M10 | ✅ | `build_ffmpeg_args`/`build_still_args` принимают разрешённый путь; shell-скрипт по-прежнему использует переносимый `ffmpeg`. Тест. |
| M11 | ✅ | `StillWorker`: таймаут, отмена, удаление `dest` перед запуском и проверка размера/кода возврата. Тест на stale-файл. |
| M12 | ✅ | CI: `uv lock --check`, `pytest -W error`, `mypy`, матрица Ubuntu/Windows/macOS; добавлены регрессионные тесты на воркеры, закрытие, пути, битые проекты, сетку. |
| M13 | ✅ | SHA-256 для PyApp и python-build-standalone, `cargo build --locked`, `pip --require-hashes` по `uv export`; локальная сборка требует хэши. |
| M14 | ✅ | `uv.lock` синхронизирован; `uv lock --check` добавлен в CI. |
| M16 | ✅ | `_parse_bool` принимает только настоящий JSON-bool. Тест. |
| M17 | ✅ | `_save_script` спрашивает подтверждение перед перезаписью существующего `.sh`. Тест. |
| P3.1 | ✅ | `_score_probe.py` удалён. |
| P3.2 | ✅ | Зазор теперь только между ячейками: фильтр обрезает хвостовой `gap`, превью не рисует внешние поля. Тесты размеров и реального ffmpeg. |
| P3.3 | ✅ | `set_probe` не двигает playhead пользователя. |
| P3.4 | ⚠️ | Блокирующее закрытие смягчено: мгновенная отмена, общий дедлайн 3 с, затем `terminate()`. Полностью неблокирующий сценарий с прогрессом не делался. |
| P3.5 | ✅ | `app.py`: лог-файл в `QStandardPaths.AppLocalDataLocation` (fallback — `tempfile.gettempdir()`), `sys.excepthook` и `threading.excepthook`. |
| P3.6 | ✅ | `tempfile.gettempdir()` вместо `/tmp`. |
| P3.7 | ✅ | `set_probe` отвергает `inf`/`nan`, `parse_probe_json` и `load_project` проверяют конечность. |
| P3.8 | ✅ | Ожидание `QProcess` при закрытии увеличено до 3 с + 1 с после `terminate()`. |

### Что осталось осознанно

- P3.4: закрытие всё ещё синхронное. Это компромисс: приложение не теряет воркеры и не падает,
  но на зависшем GPU-стеке окно может задержаться до 3 с. Неблокирующий диалог с прогрессом —
  отдельная задача, требующая переработки жизненного цикла окна.
- Сквозной тест сборки PyApp в CI не запускается (нужен Rust и скачивание дистрибутивов);
  проверены синтаксис скриптов и логика хэширования, а сам релизный workflow собирает бинарники
  на матрице ОС.

---

## 11. Локализация

- Английский — исходный язык: все пользовательские строки в `src/video_puzzle` написаны
  по-английски и проходят через `video_puzzle.i18n.tr` / `trn`.
- Русский каталог — обычный Python-модуль `translations/ru.py`; внешние `.ts`/`.qm` и
  лингвистические инструменты для строк приложения не нужны.
- Язык выбирается по цепочке: `QSettings["language"]` → `VIDEO_PUZZLE_LANG` / `LANGUAGE` /
  `LC_ALL` / `LC_MESSAGES` / `LANG` → `QLocale.system()` → Python `locale` → английский.
  В боковой панели есть переключатель с немедленной перерисовкой и сохранением выбора.
- Для стандартных кнопок Qt (`Да`/`Нет`/`Сохранить`/`Отмена`) и системных диалогов
  подключается штатный `qtbase_ru.qm` из PySide6 (`ui/qt_translations.py`). Это не заменяет
  каталог приложения, а дополняет его.
- Технические подробности (`stderr` ffmpeg/ffprobe, `JSONDecodeError`, `OSError`) больше не
  попадают в основной текст ошибки: исключения хранят их в `.detail`, а UI показывает
  локализованное сообщение. Для ошибок рендера сырой лог доступен в «подробностях» диалога.
- `tests/test_i18n.py` проверяет определение языка, плюрализацию, полноту каталога,
  совпадение `str.format`-плейсхолдеров, отсутствие кириллицы в исходных строках, русские
  сообщения доменных ошибок и переключение языка в живом окне (включая стандартные кнопки Qt).

Проверка wheel: `video_puzzle/i18n.py`, `video_puzzle/translations/ru.py` и
`video_puzzle/ui/qt_translations.py` входят в `video_puzzle-0.1.1-py3-none-any.whl`.

### Изменённые файлы

`src/video_puzzle/{app,encoders,ffmpeg_script,i18n,layout,probe,progress,project,state,sync,thumbnails,cache}.py`,
`src/video_puzzle/translations/{__init__,ru}.py`,
`src/video_puzzle/ui/{main_window,preview_canvas,qt_translations,slot_widget,timeline,trim_editor}.py`,
`tests/*`, `pyproject.toml`, `uv.lock`, `README.md`, `.github/workflows/{ci,release}.yml`,
`scripts/{build-pyapp,prepare-pyapp-distribution}.sh`.
