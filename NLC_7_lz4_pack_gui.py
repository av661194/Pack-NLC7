#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==============================================================================
# СЕКЦИЯ 1: ИМПОРТ БИБЛИОТЕК И НАСТРОЙКИ
# ==============================================================================
import argparse
import os
import queue
import struct
import sys
import threading
import zlib
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import lz4.block as _lz4
except ImportError:
    _lz4 = None

# Форматы файлов, которые уже сжаты (повторное сжатие не дает выигрыша) или которые идут в многопоточном режиме
#NO_COMPRESS_EXT = {".ogg", ".ogm", ".avi", ".mp4", ".bik", ".dds", ".thm"}
NO_COMPRESS_EXT = {".ogm", ".avi", ".mp4", ".bik", ".geom", ".geomx"}

MIN_VOL_MB = 32
MAX_VOL_MB = 1920
DEFAULT_VOL_MB = 640


def normalize_volume_mb(raw_val) -> int:
    """Возвращает размер тома в МБ (32..1920). Если пусто или вне границ — 640 МБ."""
    if raw_val is None:
        return DEFAULT_VOL_MB
    s = str(raw_val).strip()
    if not s:
        return DEFAULT_VOL_MB
    try:
        val = int(float(s))
    except ValueError:
        return DEFAULT_VOL_MB
    if val < MIN_VOL_MB or val > MAX_VOL_MB:
        return DEFAULT_VOL_MB
    return val


def make_volume_name(base_path: str, vol_idx: int, total_vols: int) -> str:
    """Формирует имя тома в стиле GSC (.db0/.db1 или .dba/.dbb) при разбиении."""
    if total_vols <= 1:
        return base_path
    root, ext = os.path.splitext(base_path)
    if ext and ext[-1].isdigit():
        # Например: gamedata.db0 -> gamedata.db0, gamedata.db1 ...
        prefix = ext[:-1]
        start_num = int(ext[-1])
        return f"{root}{prefix}{start_num + vol_idx}"
    if ext and ext[-1].isalpha():
        # Например: gamedata.dba -> gamedata.dba, gamedata.dbb ...
        prefix = ext[:-1]
        base_ord = ord(ext[-1])
        return f"{root}{prefix}{chr(base_ord + vol_idx)}"
    return f"{base_path}{vol_idx}"


# ==============================================================================
# СЕКЦИЯ 2: ФУНКЦИЯ ОБЕРТКИ В ФОРМАТ LZ4F (NLC 7)
# ==============================================================================
def wrap_lz4(data: bytes) -> bytes:
    """Сжимает блок данных алгоритмом LZ4 и добавляет 9-байтный заголовок NLC 7."""
    if _lz4 is None:
        raise RuntimeError("Библиотека 'lz4' не установлена. Выполните: pip install lz4")
    comp_block = _lz4.compress(data, store_size=False)
    header = b"LZ4F" + struct.pack("<IB", len(data), 0)
    return header + comp_block


# ==============================================================================
# СЕКЦИЯ 3: ЯДРО СБОРКИ МНОГОТОМНОГО АРХИВА (.DB / .XDB)
# ==============================================================================
def _write_single_volume(out_file: str, vol_records: list, vol_data: bytearray, compress_table: bool, no_compress: bool):
    """Записывает один готовый том архива NLC 7 (Чанк 1 -> USERDATA -> Чанк 0)."""
    # Собираем уникальные папки (включая все родительские) только для файлов этого тома
    vol_dirs = set()
    for rel_path, *_ in vol_records:
        cur = os.path.dirname(rel_path)
        while cur:
            vol_dirs.add(cur)
            cur = os.path.dirname(cur)
    sorted_dirs = sorted(vol_dirs)

    def make_table(base_data_offset: int) -> bytes:
        tbl = bytearray()
        for d in sorted_dirs:
            dir_name = d if d.endswith("\\") else d + "\\"
            name_bytes = dir_name.encode("cp1251")
            body_len = 22 + len(name_bytes)
            dir_header = (b"\x00" * 12) + (b"\xff" * 8)
            tbl.extend(struct.pack("<HH", body_len, 0x81) + dir_header + name_bytes)

        for rel_path, real_size, comp_size, crc, mtime, r_off in vol_records:
            name_bytes = rel_path.encode("cp1251")
            body_len = 26 + len(name_bytes)
            size_field = body_len - 4
            abs_offset = base_data_offset + r_off
            file_flags = 0x0032 if comp_size < real_size else 0x0022
            tbl.extend(
                struct.pack("<HH", size_field, file_flags)
                + struct.pack("<6I", real_size, comp_size, crc, mtime, 0, abs_offset)
                + name_bytes
            )
        return bytes(tbl)

    userdata_sig = b"USERDATA:ALIGN_CHUNK\x00\x00\x00\x00"
    aligned_data_start = 2048

    for _ in range(3):
        final_table_raw = make_table(aligned_data_start)
        if compress_table and not no_compress and len(final_table_raw) > 64:
            cand = wrap_lz4(final_table_raw)
            if len(cand) < len(final_table_raw):
                chunk1_id = 1 | 0x80000000
                chunk1_data = cand
            else:
                chunk1_id = 1
                chunk1_data = final_table_raw
        else:
            chunk1_id = 1
            chunk1_data = final_table_raw

        pos_after_chunk1 = 8 + len(chunk1_data)
        min_pos_after_ud = pos_after_chunk1 + 8 + len(userdata_sig) + 8
        aligned_data_start = ((min_pos_after_ud + 2047) // 2048) * 2048

    pad_len = aligned_data_start - min_pos_after_ud
    userdata_payload = userdata_sig + (b"\x00" * pad_len)
    userdata_chunk_id = 2 | 0x91000000

    with open(out_file, "wb") as out:
        out.write(struct.pack("<II", chunk1_id, len(chunk1_data)))
        out.write(chunk1_data)

        out.write(struct.pack("<II", userdata_chunk_id, len(userdata_payload)))
        out.write(userdata_payload)

        out.write(struct.pack("<II", 0, len(vol_data)))
        out.write(vol_data)


def build_archive(
    src_dir: str,
    out_file: str,
    compress_table: bool = True,
    no_compress: bool = False,
    max_vol_mb=DEFAULT_VOL_MB,
    progress_cb=None,
    stop_flag=None,
):
    """Сканирует папку, сжимает файлы и автоматически разбивает результат на тома."""
    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        raise FileNotFoundError(f"Папка не найдена: {src_dir}")

    vol_limit_mb = normalize_volume_mb(max_vol_mb)
    vol_limit_bytes = vol_limit_mb * 1024 * 1024

    files = []
    for root, _, fnames in os.walk(src_dir):
        for fn in fnames:
            full_path = os.path.join(root, fn)
            rel_path = os.path.relpath(full_path, src_dir).replace("/", "\\").lower()
            files.append((rel_path, full_path))

    files.sort(key=lambda x: x[0].lower())
    total_files = len(files)
    if progress_cb:
        progress_cb("init", (total_files, vol_limit_mb))

    # Разбиваем поток упакованных файлов на тома по лимиту vol_limit_bytes
    volumes = []  # список кортежей (vol_records, vol_data)
    cur_records = []
    cur_data = bytearray()
    cur_offset = 0
    cur_table_est = 4096  # запас под заголовок, таблицу файлов и выравнивание 2048 байт

    for idx, (rel_path, full_path) in enumerate(files, 1):
        if stop_flag and stop_flag.is_set():
            return []

        with open(full_path, "rb") as f:
            raw = f.read()

        real_size = len(raw)
        crc = zlib.crc32(raw) & 0xFFFFFFFF
        mtime = int(os.path.getmtime(full_path))
        ext = os.path.splitext(rel_path)[1].lower()

        if no_compress or real_size < 64 or ext in NO_COMPRESS_EXT:
            packed = raw
        else:
            candidate = wrap_lz4(raw)
            packed = candidate if len(candidate) < real_size else raw

        comp_size = len(packed)
        rec_est = 30 + len(rel_path)

        # Если текущий том не пуст и добавление файла превысит лимит тома — закрываем текущий том
        if cur_records and (cur_offset + comp_size + cur_table_est + rec_est > vol_limit_bytes):
            volumes.append((cur_records, cur_data))
            cur_records = []
            cur_data = bytearray()
            cur_offset = 0
            cur_table_est = 4096

        cur_data.extend(packed)
        cur_records.append((rel_path, real_size, comp_size, crc, mtime, cur_offset))
        cur_offset += comp_size
        cur_table_est += rec_est

        if progress_cb:
            progress_cb("step", (idx, total_files, rel_path))

    if cur_records or not volumes:
        volumes.append((cur_records, cur_data))

    created_files = []
    total_vols = len(volumes)
    for v_idx, (v_recs, v_data) in enumerate(volumes):
        if stop_flag and stop_flag.is_set():
            for p in created_files:
                if os.path.exists(p):
                    os.remove(p)
            return []
        vol_path = make_volume_name(out_file, v_idx, total_vols)
        if progress_cb:
            progress_cb("vol", (v_idx + 1, total_vols, vol_path))
        _write_single_volume(vol_path, v_recs, v_data, compress_table, no_compress)
        created_files.append(vol_path)

    return created_files

# ==============================================================================
# СЕКЦИЯ 4: ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (GUI НА TKINTER)
# ==============================================================================
class PackerApp(tk.Tk):
    """Главное окно программы с выбором папок, режимов сжатия, размера тома и логом."""

    def __init__(self):
        super().__init__()
        self.title("NLC 7 DB LZ4 Packer")
        self.geometry("780x530")
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        """Создает и размещает все виджеты интерфейса."""
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="x")

        # Выбор исходной папки (gamedata)
        ttk.Label(frm, text="Исходная папка:").grid(row=0, column=0, sticky="w")
        self.var_src = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_src).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Обзор...", command=self._pick_src).grid(row=0, column=2)

        # Выбор выходного файла (.db / .xdb)
        ttk.Label(frm, text="Выходной архив:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_out = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_out).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(frm, text="Сохранить как...", command=self._pick_out).grid(row=1, column=2, pady=(6, 0))

        # Поле размера тома (32..1920 МБ, пустое или вне пределов = 640 МБ)
        ttk.Label(frm, text="Размер тома (МБ):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        vol_box = ttk.Frame(frm)
        vol_box.grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        self.var_vol_mb = tk.StringVar(value="")
        ttk.Entry(vol_box, textvariable=self.var_vol_mb, width=10).pack(side="left")
        ttk.Label(
            vol_box,
            text="от 32 до 1920 (если пусто или вне пределов - 640 МБ по умолчанию)",
        ).pack(side="left", padx=8)

        frm.columnconfigure(1, weight=1)

        # Переключатели режима упаковки
        opts = ttk.Frame(self, padding=(10, 6))
        opts.pack(fill="x")

        self.var_no_comp = tk.BooleanVar(value=False)
        self.var_comp_table = tk.BooleanVar(value=True)

        ttk.Radiobutton(
            opts,
            text="Со сжатием (LZ4F)",
            variable=self.var_no_comp,
            value=False,
            command=self._on_mode_change,
        ).pack(side="left")

        ttk.Radiobutton(
            opts,
            text="Без сжатия (упаковать как есть)",
            variable=self.var_no_comp,
            value=True,
            command=self._on_mode_change,
        ).pack(side="left", padx=(12, 16))

        self.chk_comp_table = ttk.Checkbutton(
            opts,
            text="Сжимать таблицу файлов",
            variable=self.var_comp_table,
        )
        self.chk_comp_table.pack(side="left")

        # Кнопки управления процессом
        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x")
        self.btn_start = ttk.Button(btns, text="Упаковать архив", command=self._start_pack)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="Стоп", command=self._stop_pack, state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        # Прогресс-бар и строка состояния
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10)

        default_status = "Готово к работе." if _lz4 else "ВНИМАНИЕ: модуль 'lz4' не найден (pip install lz4)!"
        self.var_status = tk.StringVar(value=default_status)
        ttk.Label(self, textvariable=self.var_status, padding=(10, 4), anchor="w").pack(fill="x")

        # Окно лога
        self.log = tk.Text(self, height=13, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _on_mode_change(self):
        """Блокирует и снимает галочку сжатия таблицы, если выбран режим 'Без сжатия'."""
        if self.var_no_comp.get():
            self.var_comp_table.set(False)
            self.chk_comp_table.configure(state="disabled")
        else:
            self.chk_comp_table.configure(state="normal")
            self.var_comp_table.set(True)

    def _log(self, text: str):
        """Добавляет строку в окно лога и прокручивает его вниз."""
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pick_src(self):
        """Диалог выбора исходной папки и автоподстановка имени выходного файла."""
        path = filedialog.askdirectory(title="Выберите папку gamedata для упаковки")
        if path:
            self.var_src.set(path)
            if not self.var_out.get():
                parent = os.path.dirname(os.path.abspath(path))
                self.var_out.set(os.path.join(parent, "gamedata.db0"))

    def _pick_out(self):
        """Диалог выбора пути и имени итогового .db / .xdb архива."""
        path = filedialog.asksaveasfilename(
            title="Сохранить архив как",
            defaultextension=".db0",
            filetypes=[("X-Ray archives", "*.db* *.xdb*"), ("All files", "*.*")],
        )
        if path:
            self.var_out.set(path)

    def _start_pack(self):
        """Проверяет введенные пути и запускает сборку архива в фоновом потоке."""
        src = self.var_src.get().strip()
        out = self.var_out.get().strip()

        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка", "Укажите существующую исходную папку.")
            return
        if not out:
            messagebox.showerror("Ошибка", "Укажите путь для сохранения архива.")
            return
        if not self.var_no_comp.get() and _lz4 is None:
            messagebox.showerror("Ошибка", "Библиотека 'lz4' не установлена.\nВыполните: pip install lz4")
            return

        vol_mb = normalize_volume_mb(self.var_vol_mb.get())
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_event.clear()
        self.progress.configure(value=0)
        self._log(f"Старт упаковки: {src} -> {out} (лимит тома: {vol_mb} МБ)")

        self.worker = threading.Thread(
            target=self._worker_thread,
            args=(src, out, self.var_comp_table.get(), self.var_no_comp.get(), vol_mb),
            daemon=True,
        )
        self.worker.start()

    def _stop_pack(self):
        """Устанавливает флаг прерывания фонового потока."""
        self.stop_event.set()
        self.var_status.set("Остановка...")

    def _worker_thread(self, src: str, out: str, comp_table: bool, no_comp: bool, vol_mb: int):
        """Фоновый поток выполнения упаковки."""
        def cb(kind, payload):
            self.q.put((kind, payload))

        try:
            created = build_archive(
                src,
                out,
                compress_table=comp_table,
                no_compress=no_comp,
                max_vol_mb=vol_mb,
                progress_cb=cb,
                stop_flag=self.stop_event,
            )
            if created:
                total_mb = sum(os.path.getsize(p) for p in created) / (1024 * 1024)
                if len(created) == 1:
                    msg = f"Готово! Создан 1 архив: {created} ({total_mb:.2f} МБ)"
                else:
                    msg = f"Готово! Создано томов: {len(created)} (общий размер: {total_mb:.2f} МБ)"
                self.q.put(("done", msg))
            else:
                self.q.put(("done", "Упаковка прервана пользователем."))
        except Exception as exc:
            self.q.put(("error", str(exc)))

    def _poll_queue(self):
        """Читает сообщения из фонового потока и обновляет GUI."""
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "init":
                    total_files, limit_mb = val
                    self.progress.configure(maximum=max(total_files, 1), value=0)
                    self._log(f"Найдено файлов: {total_files}, макс. размер тома: {limit_mb} МБ")
                elif kind == "step":
                    idx, total, rel = val
                    self.progress.configure(value=idx)
                    if idx % 50 == 0 or idx == total:
                        self.var_status.set(f"Сжатие файлов: {idx}/{total} — {rel}")
                elif kind == "vol":
                    v_num, v_tot, v_path = val
                    self._log(f"Запись тома {v_num}/{v_tot}: {os.path.basename(v_path)}")
                elif kind == "done":
                    self.var_status.set(val)
                    self._log(val)
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                elif kind == "error":
                    self.var_status.set(f"Ошибка: {val}")
                    self._log(f"ОШИБКА: {val}")
                    messagebox.showerror("Ошибка упаковки", val)
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


# ==============================================================================
# СЕКЦИЯ 5: ТОЧКА ВХОДА (CLI И GUI)
# ==============================================================================
def main():
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="Упаковщик gamedata в формат .db / .xdb для NLC 7 (LZ4F)")
        parser.add_argument("input_dir", help="Путь к исходной папке gamedata")
        parser.add_argument("output_db", help="Имя выходного архива (например, gamedata.db0)")
        parser.add_argument("--no-compress", action="store_true", help="Не сжимать файлы")
        parser.add_argument("--raw-table", action="store_true", help="Не сжимать таблицу файлов")
        parser.add_argument(
            "--split",
            type=int,
            default=DEFAULT_VOL_MB,
            help="Макс. размер тома в МБ (32..1920, по умолчанию 640)",
        )
        args = parser.parse_args()
        
        def cli_cb(kind, payload):
            if kind == "init":
                print(f"Найдено файлов: {payload}, лимит тома: {payload} МБ")
            elif kind == "vol":
                print(f"Запись тома {payload}/{payload}: {payload}")
        created = build_archive(
            src_dir=args.input_dir,
            out_file=args.output_db,
            compress_table=not args.raw_table,
            no_compress=args.no_compress,
            max_vol_mb=args.split,
            progress_cb=cli_cb,
        )
        total_mb = sum(os.path.getsize(p) for p in created) / (1024 * 1024)
        print(f"Готово! Создано томов: {len(created)} ({total_mb:.2f} МБ)")
        return
    # Если аргументов нет — запускаем графическое окно
    app = PackerApp()
    app.mainloop()

if __name__ == "__main__":
    main()
