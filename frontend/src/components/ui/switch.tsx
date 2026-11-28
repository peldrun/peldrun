// frontend/src/components/ui/switch.tsx

"use client";

/**
 * Accessible on/off switch.
 *
 * Minimal role="switch" button with a CSS-only knob. No external
 * dependency. Used by the project instructions form to toggle
 * `project_instructions_enabled`.
 *
 * Direction note: the knob uses a physical `translate-x` offset.
 * The project does not currently run a global RTL layout, so the
 * physical translation matches the visual order. If a future
 * locale toggle flips the page to RTL, revisit this component and
 * either add `rtl:` variants or switch to `left`/`inset-inline`.
 */

interface SwitchProps {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  /** Accessible name (e.g. "Enable project instructions"). */
  label: string;
  disabled?: boolean;
}

export function Switch({
  checked,
  onCheckedChange,
  label,
  disabled,
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={[
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
        checked
          ? "bg-emerald-500 hover:bg-emerald-600"
          : "bg-muted-foreground/30 hover:bg-muted-foreground/50",
        disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
      ].join(" ")}
    >
      <span
        className={[
          "inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        ].join(" ")}
      />
    </button>
  );
}
