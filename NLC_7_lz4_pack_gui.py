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

# Форматы файлов, которые уже сжаты (их повторное сжатие не дает выигрыша)
NO_COMPRESS_EXT = {".ogg", ".ogm", ".avi", ".mp4", ".bik", ".dds", ".thm"}


# ==============================================================================
# СЕКЦИЯ 2: ФУНКЦИЯ ОБЕРТКИ В ФОРМАТ LZ4F (NLC 7)
# ==============================================================================
def wrap_lz4(data: bytes) -> bytes:
    """Сжимает блок данных алгоритмом LZ4 и добавляет 9-байтный заголовок NLC 7."""
    if _lz4 is None:
        raise RuntimeError("Библиотека 'lz4' не установлена. Выполните: pip install lz4")
    # Сжимаем в raw-блок без стандартного заголовка размера
    comp_block = _lz4.compress(data, store_size=False)
    # Формируем заголовок: 4 байта 'LZ4F' + 4 байта исходного размера + 1 байт флага (0x00)
    header = b"LZ4F" + struct.pack("<IB", len(data), 0)
    return header + comp_block


# ==============================================================================
# СЕКЦИЯ 3: ЯДРО СБОРКИ АРХИВА (.DB / .XDB)
# ==============================================================================
def build_archive(
    src_dir: str,
    out_file: str,
    compress_table: bool = True,
    no_compress: bool = False,
    progress_cb=None,
    stop_flag=None,
):
    """Собирает архив .db / .xdb строго в формате NLC 7 (Чанк 1 -> USERDATA:ALIGN_CHUNK -> Чанк 0)."""
    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        raise FileNotFoundError(f"Папка не найдена: {src_dir}")

    dirs = set()
    files = []

    for root, _, fnames in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        if rel_root != ".":
            dirs.add(rel_root.replace("/", "\\").lower())
        for fn in fnames:
            full_path = os.path.join(root, fn)
            rel_path = os.path.relpath(full_path, src_dir).replace("/", "\\").lower()
            files.append((rel_path, full_path))

    dirs = sorted(dirs)
    files.sort(key=lambda x: x[0].lower())

    total_files = len(files)
    if progress_cb:
        progress_cb("init", (total_files, len(dirs)))

    # 1. Сначала пакуем все файлы в буфер Чанка 0 и запоминаем относительные смещения
    data_chunks = bytearray()
    rel_offset = 0
    file_records = []

    for idx, (rel_path, full_path) in enumerate(files, 1):
        if stop_flag and stop_flag.is_set():
            return False

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
        data_chunks.extend(packed)

        file_records.append((rel_path, real_size, comp_size, crc, mtime, rel_offset))
        rel_offset += comp_size

        if progress_cb:
            progress_cb("step", (idx, total_files, rel_path))

    # 2. Вспомогательная функция сборки таблицы файлов с учетом базового смещения Чанка 0
    def make_table(base_data_offset: int) -> bytes:
        tbl = bytearray()
        for d in dirs:
            # В оригинале NLC 7 пути директорий в таблице оканчиваются на обратный слеш '\'
            dir_name = d if d.endswith("\\") else d + "\\"
            name_bytes = dir_name.encode("cp1251")
            body_len = 22 + len(name_bytes)
            tbl.extend(struct.pack("<HH", body_len, 0x81) + (b"\x00" * 20) + name_bytes)

        for rel_path, real_size, comp_size, crc, mtime, r_off in file_records:
            name_bytes = rel_path.encode("cp1251")
            body_len = 26 + len(name_bytes)
            size_field = body_len - 4
            abs_offset = base_data_offset + r_off
            tbl.extend(
                struct.pack("<HH", size_field, 0x00)
                + struct.pack("<6I", real_size, comp_size, crc, mtime, 0, abs_offset)
                + name_bytes
            )
        return bytes(tbl)

    # 3. Точный расчет выравнивания (2048 байт) с учетом реального размера сжатой таблицы
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

    # 4. Записываем архив: Чанк 1 -> Чанк USERDATA -> Чанк 0
    with open(out_file, "wb") as out:
        out.write(struct.pack("<II", chunk1_id, len(chunk1_data)))
        out.write(chunk1_data)

        out.write(struct.pack("<II", userdata_chunk_id, len(userdata_payload)))
        out.write(userdata_payload)

        out.write(struct.pack("<II", 0, len(data_chunks)))
        out.write(data_chunks)

    return True
    # ==============================================================================
# СЕКЦИЯ 4: ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (GUI НА TKINTER)
# ==============================================================================
class PackerApp(tk.Tk):
    """Главное окно программы с выбором папок, настройками, прогресс-баром и логом."""

    def __init__(self):
        super().__init__()
        self.title("X-Ray DB LZ4 Packer (NLC 7)")
        self.geometry("760x500")
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        """Создает и размещает все виджеты (поля ввода, кнопки, чекбоксы, лог)."""
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
        frm.columnconfigure(1, weight=1)

        # Чекбоксы параметров упаковки
        opts = ttk.Frame(self, padding=(10, 4))
        opts.pack(fill="x")
        self.var_comp_table = tk.BooleanVar(value=True)
        self.var_no_comp = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Сжимать таблицу файлов (LZ4F)", variable=self.var_comp_table).pack(side="left")
        ttk.Checkbutton(opts, text="Без сжатия (упаковать как есть)", variable=self.var_no_comp).pack(side="left", padx=16)

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

        # Текстовое окно лога
        self.log = tk.Text(self, height=14, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

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
                self.var_out.set(os.path.join(parent, "gamedata.dbz"))

    def _pick_out(self):
        """Диалог выбора пути и имени итогового .db / .xdb архива."""
        path = filedialog.asksaveasfilename(
            title="Сохранить архив как",
            defaultextension=".dbz",
            filetypes=[("X-Ray DB/XDB", "*.db* *.xdb*"), ("All files", "*.*")],
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

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_event.clear()
        self.progress.configure(value=0)
        self._log(f"Старт упаковки: {src} -> {out}")

        self.worker = threading.Thread(
            target=self._worker_thread,
            args=(src, out, self.var_comp_table.get(), self.var_no_comp.get()),
            daemon=True,
        )
        self.worker.start()

    def _stop_pack(self):
        """Устанавливает флаг прерывания фонового потока."""
        self.stop_event.set()
        self.var_status.set("Остановка...")

    def _worker_thread(self, src: str, out: str, comp_table: bool, no_comp: bool):
        """Фоновый поток выполнения упаковки, чтобы интерфейс не зависал."""
        def cb(kind, payload):
            self.q.put((kind, payload))

        try:
            ok = build_archive(src, out, comp_table, no_comp, progress_cb=cb, stop_flag=self.stop_event)
            if ok:
                size_mb = os.path.getsize(out) / (1024 * 1024)
                self.q.put(("done", f"Готово! Архив: {out} ({size_mb:.2f} МБ)"))
            else:
                if os.path.exists(out):
                    os.remove(out)
                self.q.put(("done", "Упаковка прервана пользователем."))
        except Exception as exc:
            self.q.put(("error", str(exc)))

    def _poll_queue(self):
        """Каждые 100 мс читает сообщения из фонового потока и обновляет GUI."""
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "init":
                    total_files, total_dirs = val
                    self.progress.configure(maximum=max(total_files, 1), value=0)
                    self._log(f"Найдено папок: {total_dirs}, файлов: {total_files}")
                elif kind == "step":
                    idx, total, rel = val
                    self.progress.configure(value=idx)
                    if idx % 50 == 0 or idx == total:
                        self.var_status.set(f"Упаковка: {idx}/{total} — {rel}")
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
# СЕКЦИЯ 5: ТОЧКА ВХОДА (ВЫБОР МЕЖДУ КОНСОЛЬНЫМ РЕЖИМОМ И GUI)
# ==============================================================================
def main():
    # Если скрипт запущен с параметрами в консоли — работаем в CLI-режиме
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="Упаковщик папок в формат .db / .xdb для NLC 7 (LZ4F)")
        parser.add_argument("input_dir", help="Путь к исходной папке (например, gamedata)")
        parser.add_argument("output_db", help="Имя выходного архива (например, gamedata.dbz)")
        parser.add_argument("--no-compress", action="store_true", help="Не сжимать файлы")
        parser.add_argument("--raw-table", action="store_true", help="Не сжимать таблицу файлов")
        args = parser.parse_args()

        def cli_cb(kind, payload):
            if kind == "init":
                print(f"Найдено папок: {payload[1]}, файлов: {payload[0]}")
            elif kind == "step" and (payload[0] % 500 == 0 or payload[0] == payload[1]):
                print(f"Обработано: {payload[0]}/{payload[1]}")

        build_archive(
            src_dir=args.input_dir,
            out_file=args.output_db,
            compress_table=not args.raw_table,
            no_compress=args.no_compress,
            progress_cb=cli_cb,
        )
        size_mb = os.path.getsize(args.output_db) / (1024 * 1024)
        print(f"Готово! Сохранено в {args.output_db} ({size_mb:.2f} МБ)")
        return

    # Если аргументов нет — запускаем графическое окно
    app = PackerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
