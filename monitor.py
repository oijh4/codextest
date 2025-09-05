import sys
import time
import platform
import subprocess
import csv
import io
import os
import shutil


def _read_windows_typeperf(interval):
    counters = [
        r"\Processor(_Total)\% Processor Time",
        r"\Memory\% Committed Bytes In Use",
    ]
    # typeperf returns one header line, one column header line, then samples
    # We collect a single sample with the given interval.
    cmd = [
        "typeperf",
        "-sc",
        "1",
        "-si",
        str(max(1, int(interval))) if interval else "1",
        *counters,
    ]
    try:
        cp = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
            check=False,
        )
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        # Last non-empty line should contain the values CSV
        if len(lines) >= 3:
            data_line = lines[-1]
            reader = csv.reader(io.StringIO(data_line))
            row = next(reader, [])
            # row example: ["09/05/2025 14:35:52.123", "2.000000", "40.000000"]
            if len(row) >= 3:
                try:
                    cpu = float(row[1])
                    mem = float(row[2])
                    return cpu, mem
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return None, None


def _read_linux_proc(interval):
    # CPU: compute from /proc/stat delta over interval
    def read_cpu_times():
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    nums = list(map(int, parts[1:]))
                    # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
                    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                    total = sum(nums)
                    return idle, total
        return None

    idle1, total1 = read_cpu_times()
    time.sleep(interval or 1)
    idle2, total2 = read_cpu_times()
    if idle1 is None or idle2 is None:
        cpu = None
    else:
        idle_delta = idle2 - idle1
        total_delta = total2 - total1
        cpu = 100.0 * (1.0 - (idle_delta / total_delta)) if total_delta > 0 else None

    # Memory: from /proc/meminfo
    mem = None
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                meminfo[k.strip()] = v.strip()
        def kb(name):
            val = meminfo.get(name, "0 kB").split()[0]
            return float(val)
        total = kb("MemTotal")
        avail = kb("MemAvailable")
        if total > 0:
            mem = 100.0 * (1.0 - (avail / total))
    except Exception:
        pass

    return cpu, mem


def _read_with_psutil(interval):
    try:
        import psutil  # type: ignore
    except Exception:
        return None, None
    cpu = psutil.cpu_percent(interval=interval or 1)
    mem = psutil.virtual_memory().percent
    return cpu, mem


def read_metrics(interval=1):
    # Prefer psutil if available
    cpu, mem = _read_with_psutil(interval)
    if cpu is not None and mem is not None:
        return cpu, mem

    if platform.system() == "Windows":
        cpu, mem = _read_windows_typeperf(interval)
        return cpu, mem

    # Linux (and possibly WSL)
    if os.path.exists("/proc/stat"):
        cpu, mem = _read_linux_proc(interval)
        return cpu, mem

    # Fallback: unknown platform
    return None, None


def format_line(cpu, mem):
    parts = []
    if cpu is not None:
        parts.append(f"CPU: {cpu:5.1f}%")
    else:
        parts.append("CPU: N/A")
    if mem is not None:
        parts.append(f"Memory: {mem:5.1f}%")
    else:
        parts.append("Memory: N/A")
    return " | ".join(parts)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Live CPU and memory usage monitor")
    parser.add_argument("--interval", "-i", type=float, default=1.0, help="Sampling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Print one sample and exit")
    parser.add_argument("--graph", action="store_true", help="Show scrolling ASCII history graph")
    parser.add_argument("--samples", type=int, default=0, help="When >0, number of samples to collect then exit")
    args = parser.parse_args()

    interval = args.interval if args.interval > 0 else 1.0

    # psutil's first cpu_percent call may need a priming read; our readers handle interval
    try:
        if args.once and not args.graph:
            cpu, mem = read_metrics(interval)
            print(format_line(cpu, mem))
            return

        if args.graph:
            # Graph mode: keep history and redraw
            cpu_hist = []
            mem_hist = []
            count = 0
            while True:
                cpu, mem = read_metrics(interval)
                # Guard None values by keeping previous or 0
                cpu_hist.append(0.0 if cpu is None else max(0.0, min(100.0, cpu)))
                mem_hist.append(0.0 if mem is None else max(0.0, min(100.0, mem)))
                count += 1

                size = shutil.get_terminal_size((80, 24))
                # Leave space for legend and axis labels
                width = max(30, size.columns - 8)
                height = max(10, min(24, size.lines - 5))

                n = min(len(cpu_hist), width)
                cpu_view = cpu_hist[-n:]
                mem_view = mem_hist[-n:]

                # Prepare empty canvas
                canvas = [[" "] * n for _ in range(height)]

                def row_for(value):
                    # value 0..100 maps bottom..top (invert for display)
                    r = int(round((value / 100.0) * (height - 1)))
                    return (height - 1) - r

                for x in range(n):
                    rc = row_for(cpu_view[x])
                    rm = row_for(mem_view[x])
                    if rc == rm:
                        canvas[rc][x] = "@"  # overlap marker
                    else:
                        canvas[rc][x] = "#"  # CPU
                        canvas[rm][x] = "*"  # MEM

                # Clear and draw
                sys.stdout.write("\x1b[2J\x1b[H")  # clear screen + home
                latest_cpu = cpu_view[-1]
                latest_mem = mem_view[-1]
                legend = f"CPU=#, MEM=* (@ overlap) | {format_line(latest_cpu, latest_mem)} | interval {interval}s"
                print(legend)

                # Draw with y-axis ticks at 100, 75, 50, 25, 0
                ticks = {0, int(height * 0.25), int(height * 0.5), int(height * 0.75), height - 1}
                for row in range(height):
                    label = "   "
                    if row in ticks:
                        # Convert row back to percent
                        val = int(round(100 * (1 - (row / (height - 1)))))
                        label = f"{val:3d}"
                    line = label + "|" + "".join(canvas[row])
                    print(line)

                print("   +" + "-" * n)
                print("".ljust(4) + "time →  (last " + str(n) + " samples)")

                if args.samples > 0 and count >= args.samples:
                    break

            print("Done.")
            return

        # Live updating single-line display (no graph)
        print("Press Ctrl+C to stop.")
        while True:
            cpu, mem = read_metrics(interval)
            line = format_line(cpu, mem)
            print("\r" + line + " " * 10, end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
