from __future__ import annotations
import os

def clear_results(filename: str) -> None:
    result_str = (
        "mono_sizes: \n"
        "bi_sizes: \n"
        "mono_frame_nums: \n"
        "bi_frame_nums: \n"
        "mono_x: \n"
        "mono_y: \n"
        "bi_x: \n"
        "bi_y: \n"
    )
    with open(filename, "w") as f:
        f.writelines(result_str)
    sidecar_path = filename + ".jsonl"
    if os.path.exists(sidecar_path):
        os.remove(sidecar_path)


def sort_results(filename: str) -> None:
    with open(filename, "r") as f:
        lines = f.readlines()

    if len(lines) < 8:
        return

    trailing_lines = lines[8:]
    headers = [line.split(":", 1)[0].strip() for line in lines[:8]]

    def split_numbers(line: str) -> list[float]:
        parts = [x for x in line.strip().split(" ") if x]
        try:
            return [float(x) for x in parts]
        except ValueError:
            return []

    def join_values(values: list[float]) -> str:
        return " ".join(str(int(v)) if float(v).is_integer() else str(v) for v in values)

    if headers == [
        "mono_sizes",
        "bi_sizes",
        "mono_frame_nums",
        "bi_frame_nums",
        "mono_x",
        "mono_y",
        "bi_x",
        "bi_y",
    ]:
        mono_sizes = split_numbers(lines[0].split(":", 1)[-1])
        bi_sizes = split_numbers(lines[1].split(":", 1)[-1])
        mono_frame_nums = split_numbers(lines[2].split(":", 1)[-1])
        bi_frame_nums = split_numbers(lines[3].split(":", 1)[-1])
        mono_x = split_numbers(lines[4].split(":", 1)[-1])
        mono_y = split_numbers(lines[5].split(":", 1)[-1])
        bi_x = split_numbers(lines[6].split(":", 1)[-1])
        bi_y = split_numbers(lines[7].split(":", 1)[-1])

        mono = list(zip(mono_sizes, mono_frame_nums, mono_x, mono_y))
        bi = list(zip(bi_sizes, bi_frame_nums, bi_x, bi_y))

        mono_sorted = sorted(mono, key=lambda row: row[0], reverse=True)
        bi_sorted = sorted(bi, key=lambda row: row[0], reverse=True)

        with open(filename, "w") as f:
            f.write(f"mono_sizes: {join_values([m[0] for m in mono_sorted])}\n")
            f.write(f"bi_sizes: {join_values([m[0] for m in bi_sorted])}\n")
            f.write(f"mono_frame_nums: {join_values([m[1] for m in mono_sorted])}\n")
            f.write(f"bi_frame_nums: {join_values([m[1] for m in bi_sorted])}\n")
            f.write(f"mono_x: {join_values([m[2] for m in mono_sorted])}\n")
            f.write(f"mono_y: {join_values([m[3] for m in mono_sorted])}\n")
            f.write(f"bi_x: {join_values([m[2] for m in bi_sorted])}\n")
            f.write(f"bi_y: {join_values([m[3] for m in bi_sorted])}\n")
            for line in trailing_lines:
                f.write(line)
        return

    if headers == [
        "0-10_sizes",
        "10-20_sizes",
        "20-30_sizes",
        "30-40_sizes",
        "0-10_frame_nums",
        "10-20_frame_nums",
        "20-30_frame_nums",
        "30-40_frame_nums",
    ]:
        labels = ["0-10", "10-20", "20-30", "30-40"]
        grouped: dict[str, list[tuple[float, float]]] = {}
        for idx, label in enumerate(labels):
            sizes = split_numbers(lines[idx].split(":", 1)[-1])
            frame_nums = split_numbers(lines[idx + 4].split(":", 1)[-1])
            grouped[label] = sorted(zip(sizes, frame_nums), key=lambda row: row[0], reverse=True)

        with open(filename, "w") as f:
            for label in labels:
                f.write(f"{label}_sizes: {join_values([row[0] for row in grouped[label]])}\n")
            for label in labels:
                f.write(f"{label}_frame_nums: {join_values([row[1] for row in grouped[label]])}\n")
            for line in trailing_lines:
                f.write(line)
