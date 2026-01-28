#!/usr/bin/env python3
"""
PyQt5 GUI for Intel Ivy Bridge CPU overclock & power management.

Reads and writes Model-Specific Registers (MSRs) to control turbo ratios
and package power limits. Requires root privileges and the 'msr' kernel module.
"""

import os
import struct
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QIntValidator
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# MSR addresses
# ---------------------------------------------------------------------------
MSR_PLATFORM_INFO = 0xCE
IA32_MISC_ENABLE = 0x1A0
MSR_TURBO_RATIO_LIMIT = 0x1AD
IA32_PERF_STATUS = 0x198
MSR_RAPL_POWER_UNIT = 0x606
MSR_PKG_POWER_LIMIT = 0x610

BCLK = 100.0  # MHz - fixed on Sandy/Ivy Bridge

FAN_PROC_PATH = "/proc/acpi/ibm/fan"
FAN_WATCHDOG_SECONDS = 30


# ---------------------------------------------------------------------------
# Low-level MSR helpers (from ivybridge-oc.py)
# ---------------------------------------------------------------------------

def read_msr(msr: int, cpu: int = 0) -> int:
    path = f"/dev/cpu/{cpu}/msr"
    fd = os.open(path, os.O_RDONLY)
    try:
        os.lseek(fd, msr, os.SEEK_SET)
        buf = os.read(fd, 8)
        return struct.unpack("<Q", buf)[0]
    finally:
        os.close(fd)


def write_msr(msr: int, value: int, cpu: int = 0) -> None:
    path = f"/dev/cpu/{cpu}/msr"
    fd = os.open(path, os.O_WRONLY)
    try:
        os.lseek(fd, msr, os.SEEK_SET)
        os.write(fd, struct.pack("<Q", value))
    finally:
        os.close(fd)


def write_msr_all_cpus(msr: int, value: int) -> None:
    for cpu in online_cpus():
        write_msr(msr, value, cpu)


def online_cpus() -> list[int]:
    cpus = []
    for entry in os.listdir("/dev/cpu"):
        if entry.isdigit():
            cpus.append(int(entry))
    return sorted(cpus)


def cpu_to_core(cpu: int) -> int:
    """Map a logical CPU number to its physical core ID."""
    try:
        with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return cpu


def bits(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def set_bits(orig: int, hi: int, lo: int, field: int) -> int:
    mask = ((1 << (hi - lo + 1)) - 1) << lo
    return (orig & ~mask) | ((field << lo) & mask)


# ---------------------------------------------------------------------------
# MSR read helpers
# ---------------------------------------------------------------------------

def read_turbo_enabled() -> bool:
    val = read_msr(IA32_MISC_ENABLE)
    return not bool(bits(val, 38, 38))


def set_turbo_enabled(enable: bool) -> None:
    for cpu in online_cpus():
        val = read_msr(IA32_MISC_ENABLE, cpu)
        if enable:
            val = set_bits(val, 38, 38, 0)
        else:
            val = set_bits(val, 38, 38, 1)
        write_msr(IA32_MISC_ENABLE, val, cpu)


def read_turbo_ratios() -> list[int]:
    raw = read_msr(MSR_TURBO_RATIO_LIMIT)
    ratios = []
    for i in range(8):
        ratios.append(bits(raw, i * 8 + 7, i * 8))
    return ratios


def write_turbo_ratios(ratios: list[int]) -> None:
    val = 0
    for i, r in enumerate(ratios):
        val = set_bits(val, i * 8 + 7, i * 8, r)
    write_msr_all_cpus(MSR_TURBO_RATIO_LIMIT, val)


def read_perf_status(cpu: int) -> int:
    val = read_msr(IA32_PERF_STATUS, cpu)
    return bits(val, 15, 8)


def read_rapl_units() -> tuple[float, float]:
    """Return (power_unit_watts, time_unit_seconds)."""
    val = read_msr(MSR_RAPL_POWER_UNIT)
    power_unit = 1.0 / (1 << bits(val, 3, 0))
    time_unit = 1.0 / (1 << bits(val, 19, 16))
    return power_unit, time_unit


def _decode_time_window(field: int, time_unit: float) -> float:
    y = field & 0x1F
    z = (field >> 5) & 0x3
    return (2 ** y) * (1.0 + z / 4.0) * time_unit


def read_power_limits(power_unit: float, time_unit: float) -> dict:
    """Read PL1/PL2 watts, time windows, and lock status."""
    raw = read_msr(MSR_PKG_POWER_LIMIT)
    return {
        "pl1_w": bits(raw, 14, 0) * power_unit,
        "pl1_enabled": bool(bits(raw, 15, 15)),
        "pl1_time": _decode_time_window(bits(raw, 23, 17), time_unit),
        "pl2_w": bits(raw, 46, 32) * power_unit,
        "pl2_enabled": bool(bits(raw, 47, 47)),
        "pl2_time": _decode_time_window(bits(raw, 55, 49), time_unit),
        "locked": bool(bits(raw, 63, 63)),
    }


def write_pl_watts(pl1_w: float | None, pl2_w: float | None,
                   power_unit: float) -> None:
    """Write PL1 and/or PL2 power limits, preserving other fields."""
    raw = read_msr(MSR_PKG_POWER_LIMIT)
    if pl1_w is not None:
        raw = set_bits(raw, 14, 0, int(round(pl1_w / power_unit)))
        raw = set_bits(raw, 15, 15, 1)  # enable PL1
    if pl2_w is not None:
        raw = set_bits(raw, 46, 32, int(round(pl2_w / power_unit)))
        raw = set_bits(raw, 47, 47, 1)  # enable PL2
    raw = set_bits(raw, 63, 63, 0)  # never set lock bit
    write_msr_all_cpus(MSR_PKG_POWER_LIMIT, raw)


# ---------------------------------------------------------------------------
# CPU temperature helpers (coretemp hwmon)
# ---------------------------------------------------------------------------

def _find_coretemp_hwmon() -> str | None:
    """Return the hwmon sysfs directory for the coretemp driver, or None."""
    base = "/sys/class/hwmon"
    try:
        for entry in os.listdir(base):
            name_path = os.path.join(base, entry, "name")
            if os.path.isfile(name_path):
                with open(name_path) as f:
                    if f.read().strip() == "coretemp":
                        return os.path.join(base, entry)
    except OSError:
        pass
    return None


def read_core_temps() -> dict[int, float]:
    """Return {core_index: temperature_celsius} from coretemp hwmon."""
    hwmon = _find_coretemp_hwmon()
    if hwmon is None:
        return {}
    temps: dict[int, float] = {}
    idx = 1
    while True:
        label_path = os.path.join(hwmon, f"temp{idx}_label")
        input_path = os.path.join(hwmon, f"temp{idx}_input")
        if not os.path.isfile(input_path):
            break
        try:
            with open(label_path) as f:
                label = f.read().strip()  # e.g. "Core 0"
            with open(input_path) as f:
                millideg = int(f.read().strip())
            if label.startswith("Core "):
                core = int(label.split()[1])
                temps[core] = millideg / 1000.0
        except (OSError, ValueError):
            pass
        idx += 1
    return temps


# ---------------------------------------------------------------------------
# ThinkPad fan control helpers (/proc/acpi/ibm/fan)
# ---------------------------------------------------------------------------

def fan_interface_available() -> bool:
    return os.path.exists(FAN_PROC_PATH)


def read_fan_status() -> dict[str, str]:
    """Parse /proc/acpi/ibm/fan, return dict with keys: status, speed, level."""
    result = {}
    with open(FAN_PROC_PATH, "r") as f:
        for line in f:
            if line.startswith("commands:"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
    return result


def write_fan_level(level: str) -> None:
    with open(FAN_PROC_PATH, "w") as f:
        f.write(f"level {level}\n")


def write_fan_watchdog(timeout: int) -> None:
    with open(FAN_PROC_PATH, "w") as f:
        f.write(f"watchdog {timeout}\n")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class OverclockWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ivy Bridge Overclock Manager")
        self.setMinimumWidth(500)

        self.power_unit, self.time_unit = read_rapl_units()
        self.cpus = online_cpus()
        self.fan_available = fan_interface_available()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        layout.addWidget(self._build_power_group())
        layout.addWidget(self._build_turbo_group())
        layout.addWidget(self._build_ratios_group())
        layout.addWidget(self._build_freq_group())
        if self.fan_available:
            layout.addWidget(self._build_fan_group())

        # Refresh timer for core speeds
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_core_speeds)
        self.timer.start(1000)

        # Initial read
        self._load_current_values()

    # -- Power limit section ------------------------------------------------

    def _build_power_group(self) -> QGroupBox:
        group = QGroupBox("Package Power Limits")
        outer = QVBoxLayout(group)

        bold = QFont()
        bold.setBold(True)
        gray_style = "color: gray; font-size: 11px;"

        # --- PL1 row ---
        pl1_row = QHBoxLayout()
        pl1_heading = QLabel("PL1 (long-term):")
        pl1_heading.setFont(bold)
        pl1_row.addWidget(pl1_heading)

        self.pl1_slider = QSlider(Qt.Horizontal)
        self.pl1_slider.setMinimum(20)
        self.pl1_slider.setMaximum(60)
        self.pl1_slider.setTickPosition(QSlider.TicksBelow)
        self.pl1_slider.setTickInterval(5)
        self.pl1_slider.valueChanged.connect(self._on_pl1_slider_changed)

        self.pl1_label = QLabel("-- W")
        self.pl1_label.setMinimumWidth(50)
        self.pl1_label.setAlignment(Qt.AlignCenter)
        self.pl1_label.setFont(bold)

        pl1_row.addWidget(QLabel("20W"))
        pl1_row.addWidget(self.pl1_slider, stretch=1)
        pl1_row.addWidget(QLabel("60W"))
        pl1_row.addWidget(self.pl1_label)
        outer.addLayout(pl1_row)

        self.pl1_time_label = QLabel("Time window: --")
        self.pl1_time_label.setStyleSheet(gray_style)
        outer.addWidget(self.pl1_time_label)

        # --- PL2 row ---
        pl2_row = QHBoxLayout()
        pl2_heading = QLabel("PL2 (short-term):")
        pl2_heading.setFont(bold)
        pl2_row.addWidget(pl2_heading)

        self.pl2_slider = QSlider(Qt.Horizontal)
        self.pl2_slider.setMinimum(20)
        self.pl2_slider.setMaximum(60)
        self.pl2_slider.setTickPosition(QSlider.TicksBelow)
        self.pl2_slider.setTickInterval(5)
        self.pl2_slider.valueChanged.connect(self._on_pl2_slider_changed)

        self.pl2_label = QLabel("-- W")
        self.pl2_label.setMinimumWidth(50)
        self.pl2_label.setAlignment(Qt.AlignCenter)
        self.pl2_label.setFont(bold)

        pl2_row.addWidget(QLabel("20W"))
        pl2_row.addWidget(self.pl2_slider, stretch=1)
        pl2_row.addWidget(QLabel("60W"))
        pl2_row.addWidget(self.pl2_label)
        outer.addLayout(pl2_row)

        self.pl2_time_label = QLabel("Time window: --")
        self.pl2_time_label.setStyleSheet(gray_style)
        outer.addWidget(self.pl2_time_label)

        # --- Apply button ---
        self.pl_apply_btn = QPushButton("Apply Power Limits")
        self.pl_apply_btn.clicked.connect(self._apply_power_limits)
        outer.addWidget(self.pl_apply_btn)

        return group

    def _on_pl1_slider_changed(self, value: int) -> None:
        self.pl1_label.setText(f"{value} W")

    def _on_pl2_slider_changed(self, value: int) -> None:
        self.pl2_label.setText(f"{value} W")

    def _apply_power_limits(self) -> None:
        pl1 = float(self.pl1_slider.value())
        pl2 = float(self.pl2_slider.value())
        try:
            write_pl_watts(pl1, pl2, self.power_unit)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to set power limits: {e}")

    # -- Turbo enable section -----------------------------------------------

    def _build_turbo_group(self) -> QGroupBox:
        group = QGroupBox("Turbo Boost")
        layout = QHBoxLayout(group)

        self.turbo_checkbox = QCheckBox("Enable Turbo Boost")
        self.turbo_checkbox.stateChanged.connect(self._on_turbo_toggled)

        layout.addWidget(self.turbo_checkbox)

        return group

    def _on_turbo_toggled(self, state: int) -> None:
        enable = state == Qt.Checked
        try:
            set_turbo_enabled(enable)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to toggle turbo: {e}")
            # Revert checkbox to actual state
            try:
                actual = read_turbo_enabled()
                self.turbo_checkbox.blockSignals(True)
                self.turbo_checkbox.setChecked(actual)
                self.turbo_checkbox.blockSignals(False)
            except Exception:
                pass

    # -- Turbo ratio section ------------------------------------------------

    def _build_ratios_group(self) -> QGroupBox:
        group = QGroupBox("Turbo Ratio Limits")
        layout = QGridLayout(group)

        self.ratio_inputs: list[QLineEdit] = []
        validator = QIntValidator(20, 42)

        for i in range(4):
            label = QLabel(f"{i + 1}-core turbo:")
            edit = QLineEdit()
            edit.setValidator(validator)
            edit.setMaximumWidth(60)
            edit.setAlignment(Qt.AlignCenter)
            ghz_label = QLabel("x100 MHz")

            layout.addWidget(label, i, 0)
            layout.addWidget(edit, i, 1)
            layout.addWidget(ghz_label, i, 2)

            self.ratio_inputs.append(edit)

        info_label = QLabel(
            "Ratio multiplier (20 = 2.0 GHz, 42 = 4.2 GHz). "
            "Lower core counts must be >= higher core counts."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(info_label, 4, 0, 1, 3)

        apply_btn = QPushButton("Apply Ratios")
        apply_btn.clicked.connect(self._apply_ratios)
        layout.addWidget(apply_btn, 5, 0, 1, 3)

        return group

    def _apply_ratios(self) -> None:
        new_ratios = []
        for i, edit in enumerate(self.ratio_inputs):
            text = edit.text().strip()
            if not text:
                QMessageBox.warning(
                    self, "Input Error",
                    f"Please enter a ratio for {i + 1}-core turbo."
                )
                return
            ratio = int(text)
            if ratio < 20 or ratio > 42:
                QMessageBox.warning(
                    self, "Input Error",
                    f"{i + 1}-core ratio must be between 20 and 42."
                )
                return
            new_ratios.append(ratio)

        # Enforce: fewer cores active -> ratio must be >= more cores active
        # i.e. 1-core >= 2-core >= 3-core >= 4-core
        for i in range(len(new_ratios) - 1):
            if new_ratios[i] < new_ratios[i + 1]:
                QMessageBox.warning(
                    self, "Input Error",
                    f"{i + 1}-core ratio ({new_ratios[i]}) must be >= "
                    f"{i + 2}-core ratio ({new_ratios[i + 1]})."
                )
                return

        # Adjust: when user edits a value, bump all core counts below it
        # to at least the edited value. This is done at apply time by
        # ensuring the constraint holds (already validated above).

        try:
            current = read_turbo_ratios()
            # Replace first 4 entries
            for i in range(4):
                current[i] = new_ratios[i]
            write_turbo_ratios(current)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to write ratios: {e}")

    # -- Core frequency display ---------------------------------------------

    def _build_freq_group(self) -> QGroupBox:
        group = QGroupBox("Current CPU Core Speeds")
        layout = QVBoxLayout(group)

        self.freq_labels: list[QLabel] = []
        mono = QFont("monospace", 10)

        for cpu in self.cpus:
            lbl = QLabel(f"CPU {cpu}: --- MHz")
            lbl.setFont(mono)
            layout.addWidget(lbl)
            self.freq_labels.append(lbl)

        return group

    def _refresh_core_speeds(self) -> None:
        temps = read_core_temps()
        for i, cpu in enumerate(self.cpus):
            try:
                ratio = read_perf_status(cpu)
                mhz = ratio * BCLK
                core_id = cpu_to_core(cpu)
                temp = temps.get(core_id)
                if temp is not None:
                    self.freq_labels[i].setText(
                        f"CPU {cpu}:  {ratio:2d}x  ({mhz:4.0f} MHz)"
                        f"    {temp:.0f}\u00b0C"
                    )
                else:
                    self.freq_labels[i].setText(
                        f"CPU {cpu}:  {ratio:2d}x  ({mhz:4.0f} MHz)"
                    )
            except Exception:
                self.freq_labels[i].setText(f"CPU {cpu}:  read error")

        self._refresh_fan_status()

    # -- Fan control section ------------------------------------------------

    def _build_fan_group(self) -> QGroupBox:
        group = QGroupBox("Fan Control")
        outer = QVBoxLayout(group)

        bold = QFont()
        bold.setBold(True)
        mono = QFont("monospace", 10)
        gray_style = "color: gray; font-size: 11px;"

        # --- Status display row ---
        status_row = QHBoxLayout()

        rpm_heading = QLabel("Current RPM:")
        rpm_heading.setFont(bold)
        status_row.addWidget(rpm_heading)

        self.fan_rpm_label = QLabel("---- RPM")
        self.fan_rpm_label.setFont(mono)
        self.fan_rpm_label.setMinimumWidth(90)
        status_row.addWidget(self.fan_rpm_label)

        status_row.addSpacing(20)

        level_heading = QLabel("Current level:")
        level_heading.setFont(bold)
        status_row.addWidget(level_heading)

        self.fan_level_label = QLabel("--")
        self.fan_level_label.setFont(mono)
        self.fan_level_label.setMinimumWidth(90)
        status_row.addWidget(self.fan_level_label)

        status_row.addStretch()
        outer.addLayout(status_row)

        # --- Separator ---
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        # --- Mode selection row ---
        mode_row = QHBoxLayout()
        mode_label = QLabel("Fan mode:")
        mode_label.setFont(bold)
        mode_row.addWidget(mode_label)

        self.fan_mode_combo = QComboBox()
        self.fan_mode_combo.addItems([
            "Auto", "Manual (level 0-7)", "Full-Speed", "Disengaged",
        ])
        self.fan_mode_combo.setMinimumWidth(180)
        self.fan_mode_combo.currentIndexChanged.connect(
            self._on_fan_mode_changed
        )
        mode_row.addWidget(self.fan_mode_combo)

        mode_row.addStretch()
        outer.addLayout(mode_row)

        # --- Manual level slider row ---
        slider_row = QHBoxLayout()
        slider_label = QLabel("Manual level:")
        slider_label.setFont(bold)
        slider_row.addWidget(slider_label)

        slider_row.addWidget(QLabel("0"))

        self.fan_level_slider = QSlider(Qt.Horizontal)
        self.fan_level_slider.setMinimum(0)
        self.fan_level_slider.setMaximum(7)
        self.fan_level_slider.setTickPosition(QSlider.TicksBelow)
        self.fan_level_slider.setTickInterval(1)
        self.fan_level_slider.setPageStep(1)
        self.fan_level_slider.setSingleStep(1)
        self.fan_level_slider.setEnabled(False)
        self.fan_level_slider.valueChanged.connect(
            self._on_fan_slider_changed
        )
        slider_row.addWidget(self.fan_level_slider, stretch=1)

        slider_row.addWidget(QLabel("7"))

        self.fan_slider_value_label = QLabel("0")
        self.fan_slider_value_label.setMinimumWidth(30)
        self.fan_slider_value_label.setAlignment(Qt.AlignCenter)
        self.fan_slider_value_label.setFont(bold)
        slider_row.addWidget(self.fan_slider_value_label)

        outer.addLayout(slider_row)

        # --- Warning label for dangerous modes ---
        self.fan_warning_label = QLabel("")
        self.fan_warning_label.setStyleSheet(
            "color: #cc6600; font-size: 11px;"
        )
        self.fan_warning_label.setWordWrap(True)
        self.fan_warning_label.setVisible(False)
        outer.addWidget(self.fan_warning_label)

        # --- Info label ---
        info_label = QLabel(
            f"Watchdog: {FAN_WATCHDOG_SECONDS}s "
            "(fan reverts to auto if app stops responding). "
            "Fan is restored to auto mode on application exit."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet(gray_style)
        outer.addWidget(info_label)

        # --- Apply button ---
        self.fan_apply_btn = QPushButton("Apply Fan Setting")
        self.fan_apply_btn.clicked.connect(self._apply_fan_setting)
        outer.addWidget(self.fan_apply_btn)

        return group

    def _on_fan_mode_changed(self, index: int) -> None:
        is_manual = (index == 1)
        self.fan_level_slider.setEnabled(is_manual)

        if index == 3:  # Disengaged
            self.fan_warning_label.setText(
                "WARNING: Disengaged mode removes all speed limits. "
                "The fan may spin beyond its rated maximum. Use with caution."
            )
            self.fan_warning_label.setVisible(True)
        elif index == 2:  # Full-Speed
            self.fan_warning_label.setText(
                "Full-speed mode runs the fan at maximum RPM. "
                "This is loud but safe."
            )
            self.fan_warning_label.setVisible(True)
        elif is_manual and self.fan_level_slider.value() <= 1:
            self.fan_warning_label.setText(
                "Warning: Very low fan levels may allow "
                "dangerous CPU temperatures."
            )
            self.fan_warning_label.setVisible(True)
        else:
            self.fan_warning_label.setVisible(False)

    def _on_fan_slider_changed(self, value: int) -> None:
        self.fan_slider_value_label.setText(str(value))
        if self.fan_mode_combo.currentIndex() == 1:
            if value <= 1:
                self.fan_warning_label.setText(
                    "Warning: Very low fan levels may allow "
                    "dangerous CPU temperatures."
                )
                self.fan_warning_label.setVisible(True)
            else:
                self.fan_warning_label.setVisible(False)

    def _apply_fan_setting(self) -> None:
        index = self.fan_mode_combo.currentIndex()

        if index == 0:
            level_str = "auto"
        elif index == 1:
            level_str = str(self.fan_level_slider.value())
        elif index == 2:
            level_str = "full-speed"
        elif index == 3:
            level_str = "disengaged"
        else:
            return

        if index == 3:
            reply = QMessageBox.warning(
                self, "Confirm Disengaged Mode",
                "Disengaged mode removes firmware speed limits "
                "on the fan.\n\nAre you sure you want to proceed?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        try:
            write_fan_level(level_str)
            if level_str != "auto":
                write_fan_watchdog(FAN_WATCHDOG_SECONDS)
            else:
                write_fan_watchdog(0)
        except PermissionError:
            QMessageBox.critical(
                self, "Permission Error",
                "Cannot write to fan control interface.\n\n"
                "Ensure thinkpad_acpi is loaded with fan_control=1:\n"
                "  modprobe thinkpad_acpi fan_control=1"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Failed to set fan level: {e}"
            )

    def _refresh_fan_status(self) -> None:
        if not self.fan_available:
            return
        try:
            status = read_fan_status()
            speed = status.get("speed", "?")
            level = status.get("level", "?")
            self.fan_rpm_label.setText(f"{speed} RPM")
            self.fan_level_label.setText(level)

            # Keep watchdog alive if fan is in non-auto mode
            if level != "auto":
                write_fan_watchdog(FAN_WATCHDOG_SECONDS)
        except Exception:
            self.fan_rpm_label.setText("read error")
            self.fan_level_label.setText("read error")

    # -- Safety: restore fan on exit ----------------------------------------

    def closeEvent(self, event) -> None:
        if self.fan_available:
            try:
                write_fan_level("auto")
                write_fan_watchdog(0)
            except Exception:
                pass
        event.accept()

    # -- Load current values ------------------------------------------------

    def _load_current_values(self) -> None:
        # Power limits
        try:
            pl = read_power_limits(self.power_unit, self.time_unit)

            pl1_clamped = max(20, min(60, int(round(pl["pl1_w"]))))
            self.pl1_slider.setValue(pl1_clamped)
            self.pl1_time_label.setText(
                f"Time window: {pl['pl1_time']:.3f}s"
            )

            pl2_clamped = max(20, min(60, int(round(pl["pl2_w"]))))
            self.pl2_slider.setValue(pl2_clamped)
            self.pl2_time_label.setText(
                f"Time window: {pl['pl2_time']:.6f}s"
            )
        except Exception:
            self.pl1_slider.setValue(35)
            self.pl2_slider.setValue(45)

        # Turbo enabled
        try:
            enabled = read_turbo_enabled()
            self.turbo_checkbox.blockSignals(True)
            self.turbo_checkbox.setChecked(enabled)
            self.turbo_checkbox.blockSignals(False)
        except Exception:
            pass

        # Turbo ratios
        try:
            ratios = read_turbo_ratios()
            for i in range(4):
                self.ratio_inputs[i].setText(str(ratios[i]))
        except Exception:
            pass

        # Core speeds
        self._refresh_core_speeds()

        # Fan control
        if self.fan_available:
            try:
                status = read_fan_status()
                level = status.get("level", "auto")
                self.fan_mode_combo.blockSignals(True)
                if level == "auto":
                    self.fan_mode_combo.setCurrentIndex(0)
                elif level == "full-speed":
                    self.fan_mode_combo.setCurrentIndex(2)
                elif level == "disengaged":
                    self.fan_mode_combo.setCurrentIndex(3)
                elif level.isdigit() and 0 <= int(level) <= 7:
                    self.fan_mode_combo.setCurrentIndex(1)
                    self.fan_level_slider.setValue(int(level))
                    self.fan_level_slider.setEnabled(True)
                else:
                    self.fan_mode_combo.setCurrentIndex(0)
                self.fan_mode_combo.blockSignals(False)

                self.fan_rpm_label.setText(
                    f"{status.get('speed', '?')} RPM"
                )
                self.fan_level_label.setText(
                    status.get("level", "?")
                )

                if level != "auto":
                    write_fan_watchdog(FAN_WATCHDOG_SECONDS)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)

    if os.geteuid() != 0:
        QMessageBox.critical(
            None, "Root Required",
            "This application must be run as root (sudo).\n\n"
            "Please restart with: sudo ./overclock.py"
        )
        sys.exit(1)

    # Ensure msr module is loaded
    if not os.path.isdir("/dev/cpu"):
        os.system("modprobe msr 2>/dev/null")
        if not os.path.isdir("/dev/cpu"):
            QMessageBox.critical(
                None, "MSR Module Missing",
                "Cannot access /dev/cpu.\n"
                "Load the msr module: modprobe msr"
            )
            sys.exit(1)

    # Check for thinkpad_acpi fan control (non-fatal)
    if not os.path.exists(FAN_PROC_PATH):
        QMessageBox.warning(
            None, "Fan Control Unavailable",
            f"Fan control interface not found at {FAN_PROC_PATH}.\n\n"
            "Fan control features will be disabled.\n"
            "To enable: modprobe thinkpad_acpi fan_control=1"
        )

    window = OverclockWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
