# Overclock

PyQt5 GUI for Intel Ivy Bridge CPU overclock and power management.

Reads and writes Model-Specific Registers (MSRs) to control turbo ratios
and package power limits. Optionally controls ThinkPad fan speed via
`/proc/acpi/ibm/fan`.

## Prerequisites

- Linux with the `msr` kernel module available
- Python 3.10+
- Root privileges (required for MSR access)
- For fan control: `thinkpad_acpi` loaded with `fan_control=1`

## Setup

Create and activate a virtual environment, then install dependencies:

```sh
python3 -m venv venv
source venv/bin/activate
pip install .
```

To install in editable/development mode instead:

```sh
pip install -e .
```

## Usage

The application must be run as root:

```sh
sudo venv/bin/python overclock.py
```

Before running, ensure the `msr` kernel module is loaded:

```sh
sudo modprobe msr
```

For ThinkPad fan control, load the `thinkpad_acpi` module with fan control enabled:

```sh
sudo modprobe thinkpad_acpi fan_control=1
```
