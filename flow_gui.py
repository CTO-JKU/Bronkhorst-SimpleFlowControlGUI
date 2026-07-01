import csv
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from tkinter import messagebox, ttk

import propar
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


class FlowUnit(Enum):
    LN_MIN = "ln/min"
    MLN_MIN = "mln/min"
    G_MIN = "g/min"


@dataclass(frozen=True)
class GasProperties:
    name: str
    symbol: str
    gcf: float
    density_25c: float  # g/L at 25 °C / 1 atm


GAS_DB: dict[str, GasProperties] = {
    "air":             GasProperties("Air",             "Air",   1.0015, 1.185),
    "ammonia":         GasProperties("Ammonia",         "NH3",   0.7807, 0.696),
    "argon":           GasProperties("Argon",           "Ar",    1.4047, 1.633),
    "carbon_dioxide":  GasProperties("Carbon Dioxide",  "CO2",   0.7526, 1.799),
    "carbon_monoxide": GasProperties("Carbon Monoxide", "CO",    1.0012, 1.145),
    "dimethylamine":   GasProperties("Dimethylamine",   "C2H7N", 0.3705, 1.843),
    "ethane":          GasProperties("Ethane",          "C2H6",  0.4998, 1.229),
    "ethylene":        GasProperties("Ethylene",        "C2H4",  0.6062, 1.147),
    "helium":          GasProperties("Helium",          "He",    1.4005, 0.164),
    "hydrogen":        GasProperties("Hydrogen",        "H2",    1.0038, 0.082),
    "methane":         GasProperties("Methane",         "CH4",   0.7787, 0.656),
    "methylamine":     GasProperties("Methylamine",     "CH5N",  0.5360, 1.269),
    "nitrogen":        GasProperties("Nitrogen",        "N2",    1.0,    1.145),
    "nitrous_oxide":   GasProperties("Nitrous Oxide",   "N2O",   0.7121, 1.799),
    "oxygen":          GasProperties("Oxygen",          "O2",    0.9779, 1.308),
    "propane":         GasProperties("Propane",         "C3H8",  0.3499, 1.802),
    "propylene":       GasProperties("Propylene",       "C3H6",  0.4048, 1.720),
}

FULL_SCALE = 32000


def flow_to_l_min(value: float, unit: FlowUnit, gas: GasProperties) -> float:
    if unit == FlowUnit.LN_MIN:
        return value
    if unit == FlowUnit.MLN_MIN:
        return value / 1000
    if unit == FlowUnit.G_MIN:
        return value / gas.density_25c


def l_min_to_flow(l_min: float, unit: FlowUnit, gas: GasProperties) -> float:
    if unit == FlowUnit.LN_MIN:
        return l_min
    if unit == FlowUnit.MLN_MIN:
        return l_min * 1000
    if unit == FlowUnit.G_MIN:
        return l_min * gas.density_25c


def convert_flow(value: float, from_unit: FlowUnit, to_unit: FlowUnit, gas: GasProperties) -> float:
    """Convert a flow value of a given gas between units."""
    return l_min_to_flow(flow_to_l_min(value, from_unit, gas), to_unit, gas)


# Cumulative amount unit implied by integrating a given flow rate unit over time.
CUMULATIVE_UNIT_LABEL = {
    FlowUnit.LN_MIN: "L",
    FlowUnit.MLN_MIN: "mL",
    FlowUnit.G_MIN: "g",
}


class MFCController:
    """
    calibration_unit — unit the max_flow figure is expressed in (matches how the
                        MFC was factory-calibrated for calibration_gas).
    working_unit      — unit used for set_flow()/read_flow() of selected_gas. This
                        can differ from calibration_unit and be changed at runtime
                        via `working_unit` property without touching the instrument.
    """

    def __init__(
        self,
        com_port: str,
        calibration_gas: str,
        max_flow: float,
        calibration_unit: FlowUnit,
        selected_gas: str,
        working_unit: FlowUnit | None = None,
    ):
        self._validate_gas(calibration_gas)
        self._validate_gas(selected_gas)
        self.com_port = com_port
        self.cal_gas = GAS_DB[calibration_gas]
        self.sel_gas = GAS_DB[selected_gas]
        self.max_flow = max_flow
        self.calibration_unit = calibration_unit
        self.working_unit = working_unit or calibration_unit

        self._max_cal_l_min = flow_to_l_min(max_flow, self.calibration_unit, self.cal_gas)
        self._gcf_correction = self.cal_gas.gcf / self.sel_gas.gcf
        self._instrument = propar.instrument(com_port)

    @property
    def max_selected_flow(self) -> float:
        """Maximum flow of the selected gas, expressed in working_unit."""
        l_min = self._max_cal_l_min * self._gcf_correction
        return l_min_to_flow(l_min, self.working_unit, self.sel_gas)

    def set_flow(self, flow: float) -> None:
        """flow is expressed in working_unit."""
        max_f = self.max_selected_flow
        if not 0 <= flow <= max_f:
            raise ValueError(f"Flow {flow:.4f} out of range [0, {max_f:.4f}]")
        flow_l_min = flow_to_l_min(flow, self.working_unit, self.sel_gas)
        cal_l_min = flow_l_min / self._gcf_correction
        self._instrument.setpoint = round((cal_l_min / self._max_cal_l_min) * FULL_SCALE)

    def read_flow(self) -> float:
        """Returns flow expressed in working_unit."""
        raw = self._instrument.measure
        cal_l_min = (raw / FULL_SCALE) * self._max_cal_l_min
        sel_l_min = cal_l_min * self._gcf_correction
        return l_min_to_flow(sel_l_min, self.working_unit, self.sel_gas)

    def stop(self) -> None:
        self._instrument.setpoint = 0

    @staticmethod
    def _validate_gas(key):
        if key not in GAS_DB:
            raise ValueError(f"Unknown gas '{key}'. Available: {list(GAS_DB)}")


# ======================================================================
# Default configuration — can also be changed in the GUI at runtime
# ======================================================================
DEFAULT_COM_PORT = "COM7"
DEFAULT_CALIBRATION_GAS = "dimethylamine"
DEFAULT_MAX_FLOW = 1.5
DEFAULT_CALIBRATION_UNIT = FlowUnit.G_MIN
DEFAULT_SELECTED_GAS = "nitrogen"
DEFAULT_WORKING_UNIT = FlowUnit.MLN_MIN
DEFAULT_TARGET_FLOW = 150.0
WINDOW_S = 120
POLL_INTERVAL_S = 1.0


class FlowMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MFC Flow Monitor")
        self.root.geometry("950x680")

        self.mfc: MFCController | None = None
        self.stop_event = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        self.data_queue: queue.Queue = queue.Queue()
        self.csv_file = None
        self.csv_writer = None

        self.elapsed_data: list[float] = []
        self.flow_data: list[float] = []
        self.cumulative_data: list[float] = []
        self.start_time = 0.0
        self.setpoint_line = None

        self._build_ui()
        self._poll_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        # Row 0: connection + calibration settings
        ttk.Label(top, text="COM Port:").grid(row=0, column=0, sticky="w")
        self.com_port_var = tk.StringVar(value=DEFAULT_COM_PORT)
        ttk.Entry(top, textvariable=self.com_port_var, width=12).grid(row=0, column=1, padx=5)

        ttk.Label(top, text="Calibration gas:").grid(row=0, column=2, sticky="w")
        self.cal_gas_var = tk.StringVar(value=DEFAULT_CALIBRATION_GAS)
        ttk.Combobox(
            top, textvariable=self.cal_gas_var, values=list(GAS_DB), width=15, state="readonly"
        ).grid(row=0, column=3, padx=5)

        ttk.Label(top, text="Max flow:").grid(row=0, column=4, sticky="w")
        self.max_flow_var = tk.StringVar(value=str(DEFAULT_MAX_FLOW))
        ttk.Entry(top, textvariable=self.max_flow_var, width=8).grid(row=0, column=5, padx=5)

        ttk.Label(top, text="Calib. unit:").grid(row=0, column=6, sticky="w")
        self.calibration_unit_var = tk.StringVar(value=DEFAULT_CALIBRATION_UNIT.value)
        ttk.Combobox(
            top, textvariable=self.calibration_unit_var,
            values=[u.value for u in FlowUnit], width=8, state="readonly",
        ).grid(row=0, column=7, padx=5)

        # Row 1: selected gas + working unit
        ttk.Label(top, text="Selected gas:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.sel_gas_var = tk.StringVar(value=DEFAULT_SELECTED_GAS)
        ttk.Combobox(
            top, textvariable=self.sel_gas_var, values=list(GAS_DB), width=15, state="readonly"
        ).grid(row=1, column=1, padx=5, pady=(8, 0))

        ttk.Label(top, text="Working unit:").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.working_unit_var = tk.StringVar(value=DEFAULT_WORKING_UNIT.value)
        self.working_unit_combo = ttk.Combobox(
            top, textvariable=self.working_unit_var,
            values=[u.value for u in FlowUnit], width=8, state="disabled",
        )
        self.working_unit_combo.grid(row=1, column=3, padx=5, pady=(8, 0))
        self.working_unit_combo.bind("<<ComboboxSelected>>", self._on_working_unit_change)

        self.max_flow_label_var = tk.StringVar(value="Max: -")
        ttk.Label(top, textvariable=self.max_flow_label_var).grid(
            row=1, column=4, columnspan=4, sticky="w", pady=(8, 0)
        )

        # Row 2: target flow
        ttk.Label(top, text="Target flow:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.target_flow_var = tk.StringVar(value=str(DEFAULT_TARGET_FLOW))
        ttk.Entry(top, textvariable=self.target_flow_var, width=10).grid(
            row=2, column=1, padx=5, pady=(8, 0), sticky="w"
        )
        self.target_flow_unit_label_var = tk.StringVar(value=DEFAULT_WORKING_UNIT.value)
        ttk.Label(top, textvariable=self.target_flow_unit_label_var).grid(
            row=2, column=2, sticky="w", pady=(8, 0)
        )
        self.set_flow_btn = ttk.Button(top, text="Set Flow", command=self.apply_flow, state=tk.DISABLED)
        self.set_flow_btn.grid(row=2, column=3, padx=5, pady=(8, 0), sticky="w")

        # Row 3: buttons
        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=3, column=0, columnspan=8, pady=(10, 0), sticky="w")

        self.connect_btn = ttk.Button(btn_frame, text="Connect", command=self.connect)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.start_btn = ttk.Button(btn_frame, text="Start", command=self.start_monitoring, state=tk.DISABLED)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_monitoring, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.status_var = tk.StringVar(value="Not connected.")
        ttk.Label(top, textvariable=self.status_var, foreground="gray").grid(
            row=4, column=0, columnspan=8, sticky="w", pady=(8, 0)
        )

        # --- Plots: flow rate (top) + cumulative amount (bottom) ---
        self.fig = Figure(figsize=(9, 6), dpi=100)
        self.ax = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212, sharex=self.ax)
        (self.line,) = self.ax.plot([], [], color="steelblue", linewidth=1.5)
        (self.cum_line,) = self.ax2.plot([], [], color="seagreen", linewidth=1.5)
        self.ax.set_ylabel("Flow")
        self.ax.grid(True, alpha=0.3)
        self.ax2.set_xlabel("Elapsed time (s)")
        self.ax2.set_ylabel("Cumulative amount")
        self.ax2.grid(True, alpha=0.3)
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Connection / control
    # ------------------------------------------------------------------

    def connect(self):
        try:
            com_port = self.com_port_var.get().strip()
            cal_gas = self.cal_gas_var.get()
            max_flow = float(self.max_flow_var.get())
            calibration_unit = FlowUnit(self.calibration_unit_var.get())
            sel_gas = self.sel_gas_var.get()
            working_unit = FlowUnit(self.working_unit_var.get())

            self.mfc = MFCController(
                com_port=com_port,
                calibration_gas=cal_gas,
                max_flow=max_flow,
                calibration_unit=calibration_unit,
                selected_gas=sel_gas,
                working_unit=working_unit,
            )
            self._refresh_max_flow_label()
            self.target_flow_unit_label_var.set(working_unit.value)
            self.status_var.set(f"Connected to {com_port}.")
            self.start_btn.config(state=tk.NORMAL)
            self.set_flow_btn.config(state=tk.NORMAL)
            self.connect_btn.config(state=tk.DISABLED)
            self.working_unit_combo.config(state="readonly")
        except Exception as exc:
            messagebox.showerror("Connection failed", str(exc))

    def _refresh_max_flow_label(self):
        if self.mfc is None:
            return
        self.max_flow_label_var.set(
            f"Max: {self.mfc.max_selected_flow:.4f} {self.mfc.working_unit.value} "
            f"{self.mfc.sel_gas.symbol}  (calibrated {self.mfc.max_flow} "
            f"{self.mfc.calibration_unit.value} {self.mfc.cal_gas.symbol})"
        )

    def _on_working_unit_change(self, _event=None):
        """Live-convert the displayed target flow value when the working unit changes."""
        if self.mfc is None:
            return
        new_unit = FlowUnit(self.working_unit_var.get())
        old_unit = self.mfc.working_unit
        if new_unit == old_unit:
            return

        try:
            current_value = float(self.target_flow_var.get())
            converted = convert_flow(current_value, old_unit, new_unit, self.mfc.sel_gas)
            self.target_flow_var.set(f"{converted:.4f}")
        except ValueError:
            pass  # leave the field as-is if it wasn't a valid number

        self.mfc.working_unit = new_unit
        self.target_flow_unit_label_var.set(new_unit.value)
        self._refresh_max_flow_label()

    def apply_flow(self):
        """Push a new setpoint to the MFC immediately — works whether monitoring
        is running or not, so the flow can be changed mid-run without stopping."""
        if self.mfc is None:
            return
        try:
            target_flow = float(self.target_flow_var.get())
            self.mfc.set_flow(target_flow)
        except Exception as exc:
            messagebox.showerror("Invalid flow", str(exc))
            return

        unit_label = self.mfc.working_unit.value
        self.status_var.set(f"Setpoint updated: {target_flow} {unit_label} {self.mfc.sel_gas.symbol}")

        # If a run is active, move the setpoint reference line instead of restarting the plot.
        if self.setpoint_line is not None:
            self.setpoint_line.set_ydata([target_flow, target_flow])
            self.setpoint_line.set_label(f"Setpoint ({target_flow} {unit_label})")
            self.ax.legend(loc="upper right")
            self.ax.relim()
            self.ax.autoscale_view(scalex=False)
            self.canvas.draw_idle()

    def start_monitoring(self):
        if self.mfc is None:
            return
        try:
            target_flow = float(self.target_flow_var.get())
            self.mfc.set_flow(target_flow)
        except Exception as exc:
            messagebox.showerror("Invalid flow", str(exc))
            return

        csv_filename = f"flow_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.csv_file = open(csv_filename, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        unit_label = self.mfc.working_unit.value
        gas_symbol = self.mfc.sel_gas.symbol
        cum_unit = CUMULATIVE_UNIT_LABEL[self.mfc.working_unit]
        self.csv_writer.writerow(
            ["timestamp", "elapsed_s", f"flow_{unit_label}_{gas_symbol}", f"cumulative_{cum_unit}_{gas_symbol}"]
        )

        self.elapsed_data.clear()
        self.flow_data.clear()
        self.cumulative_data.clear()
        self.start_time = time.monotonic()
        self.stop_event.clear()

        self.ax.clear()
        self.ax2.clear()
        (self.line,) = self.ax.plot([], [], color="steelblue", linewidth=1.5, label=gas_symbol)
        self.setpoint_line = self.ax.axhline(
            target_flow, color="tomato", linestyle="--", linewidth=1,
            label=f"Setpoint ({target_flow} {unit_label})",
        )
        self.ax.set_ylabel(f"Flow ({unit_label})")
        self.ax.set_title(f"{self.mfc.sel_gas.name}  |  {self.mfc.com_port}  |  {csv_filename}")
        self.ax.legend(loc="upper right")
        self.ax.grid(True, alpha=0.3)

        (self.cum_line,) = self.ax2.plot([], [], color="seagreen", linewidth=1.5, label=gas_symbol)
        self.ax2.set_xlabel("Elapsed time (s)")
        self.ax2.set_ylabel(f"Cumulative ({cum_unit})")
        self.ax2.legend(loc="upper left")
        self.ax2.grid(True, alpha=0.3)

        self.canvas.draw_idle()

        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

        self.status_var.set(f"Monitoring... logging to {csv_filename}")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.working_unit_combo.config(state="disabled")

    def stop_monitoring(self):
        # Close the valve first, immediately — don't make it wait on the
        # background thread's read/sleep cycle (that could take up to
        # POLL_INTERVAL_S + join timeout before the setpoint is zeroed).
        if self.mfc is not None:
            try:
                self.mfc.stop()
            except Exception as exc:
                messagebox.showerror("Failed to close valve", str(exc))

        self.stop_event.set()
        if self.monitor_thread is not None:
            self.monitor_thread.join(timeout=5)

        if self.csv_file is not None:
            self.csv_file.close()
            self.csv_file = None

        self.setpoint_line = None

        self.status_var.set(f"Stopped. {len(self.flow_data)} samples recorded.")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.working_unit_combo.config(state="readonly")

    # ------------------------------------------------------------------
    # Background thread — puts readings on a queue, never touches Tk directly
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                elapsed = time.monotonic() - self.start_time
                flow = self.mfc.read_flow()
                ts = datetime.now().isoformat()
                self.data_queue.put((elapsed, flow, ts))
            except Exception as exc:
                self.data_queue.put(("error", str(exc), None))
            self.stop_event.wait(timeout=POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Main-thread queue poller — safe to touch Tk/matplotlib here
    # ------------------------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                item = self.data_queue.get_nowait()
                if item[0] == "error":
                    self.status_var.set(f"Read error: {item[1]}")
                    continue

                elapsed, flow, ts = item

                # Trapezoidal integration of flow rate (per minute) over elapsed time (s)
                if self.elapsed_data:
                    dt_min = (elapsed - self.elapsed_data[-1]) / 60
                    delta_amount = (flow + self.flow_data[-1]) / 2 * dt_min
                    cumulative = self.cumulative_data[-1] + delta_amount
                else:
                    cumulative = 0.0

                self.elapsed_data.append(elapsed)
                self.flow_data.append(flow)
                self.cumulative_data.append(cumulative)

                if self.csv_writer is not None:
                    self.csv_writer.writerow([ts, f"{elapsed:.2f}", f"{flow:.4f}", f"{cumulative:.4f}"])
                    self.csv_file.flush()

                self.line.set_data(self.elapsed_data, self.flow_data)
                self.ax.set_xlim(max(0, elapsed - WINDOW_S), elapsed + 2)
                self.ax.relim()
                self.ax.autoscale_view(scalex=False)

                self.cum_line.set_data(self.elapsed_data, self.cumulative_data)
                self.ax2.set_xlim(0, elapsed + 2)
                self.ax2.relim()
                self.ax2.autoscale_view(scalex=False)

                self.canvas.draw_idle()
        except queue.Empty:
            pass
        finally:
            self.root.after(200, self._poll_queue)

    def _on_close(self):
        if self.monitor_thread is not None and self.monitor_thread.is_alive():
            self.stop_monitoring()
        self.root.destroy()


def main():
    root = tk.Tk()
    FlowMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
