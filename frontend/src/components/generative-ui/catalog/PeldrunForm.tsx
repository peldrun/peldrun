// frontend/src/components/generative-ui/catalog/PeldrunForm.tsx
"use client";

import React, { useState } from "react";
import type { PeldrunFormProps } from "../types";

export function PeldrunForm({
  title,
  description,
  fields = [],
  submitLabel = "Submit",
  onSubmit,
}: PeldrunFormProps): React.JSX.Element {
  const [formData, setFormData] = useState<Record<string, string | boolean>>({});

  const handleTextChange = (name: string, value: string) => {
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleCheckboxChange = (name: string, checked: boolean) => {
    setFormData((prev) => ({ ...prev, [name]: checked }));
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (onSubmit) {
      onSubmit(formData);
    } else {
      console.info("[PeldrunForm Submitted]", formData);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="my-2 p-3.5 rounded-xl border border-border/70 bg-card/60 space-y-3"
    >
      {(title || description) && (
        <div className="space-y-0.5">
          {title && <div className="text-xs font-semibold text-foreground">{title}</div>}
          {description && (
            <div className="text-[11px] text-muted-foreground">{description}</div>
          )}
        </div>
      )}

      <div className="space-y-2.5">
        {fields.map((field) => {
          const rawValue = formData[field.name];
          const stringValue = typeof rawValue === "string" ? rawValue : "";
          const booleanValue = typeof rawValue === "boolean" ? rawValue : false;

          return (
            <div key={field.name} className="space-y-1">
              <label className="text-[11px] font-medium text-foreground block">
                {field.label}
              </label>
              {field.type === "select" ? (
                <select
                  value={stringValue}
                  onChange={(e) => handleTextChange(field.name, e.target.value)}
                  className="w-full bg-background border border-border rounded-lg px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
                >
                  <option value="">Select option...</option>
                  {field.options?.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              ) : field.type === "checkbox" ? (
                <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={booleanValue}
                    onChange={(e) => handleCheckboxChange(field.name, e.target.checked)}
                    className="rounded border-border text-primary focus:ring-primary"
                  />
                  <span>{field.placeholder || "Enable"}</span>
                </label>
              ) : (
                <input
                  type={field.type || "text"}
                  placeholder={field.placeholder}
                  value={stringValue}
                  onChange={(e) => handleTextChange(field.name, e.target.value)}
                  className="w-full bg-background border border-border rounded-lg px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
                />
              )}
            </div>
          );
        })}
      </div>

      <button
        type="submit"
        className="px-3 py-1.5 bg-primary text-primary-foreground text-xs font-medium rounded-lg hover:bg-primary/90 transition-all shadow-xs cursor-pointer"
      >
        {submitLabel}
      </button>
    </form>
  );
}
