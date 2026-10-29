import React from "react";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "outline" | "glow" | "ghost" | "danger" | "destructive" | "save" | "rb";
  size?: "sm" | "md" | "lg";
  children?: React.ReactNode;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className = "", variant = "secondary", size = "md", children, ...props }, ref) => {
    const base = "inline-flex items-center justify-center font-medium transition-colors focus-visible:outline-none disabled:opacity-50 disabled:pointer-events-none rounded-xs ";

    const variants: Record<string, string> = {
      primary: "bg-black hover:text-primary-foreground hover:bg-primary/90 shadow-peldrun-xs",
      secondary: "bg-rb hover:bg-black text-[#fff] shadow-peldrun-xs",
      outline: "border border-rb bg-transparent hover:bg-rb text-black hover:text-[#fff] shadow-peldrun-xs ",
      glow: "bg-primary text-primary-foreground shadow-[0_0_15px_rgba(136,81,255,0.4)] hover:shadow-[0_0_20px_rgba(136,81,255,0.6)]",
      ghost: "hover:bg-muted text-muted-foreground hover:text-foreground",
      danger: "bg-destructive text-destructive-foreground hover:bg-destructive/90 shadow-peldrun-xs",
      destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90 shadow-peldrun-xs",
      save: "bg-[#101010] text-[#fff] hover:text-primary-foreground hover:bg-primary/90 shadow-peldrun-xs",
      rb: "border border-rb bg-rb hover:bg-rb/90 text-[#fff] shadow-peldrun-xs",
    };

    const sizes: Record<string, string> = {
      sm: "h-7 px-2.5 text-xs",
      md: "h-9 px-4 text-sm",
      lg: "h-11 px-6 text-base",
    };

    const variantClass = variants[variant] || variants.secondary;
    const sizeClass = sizes[size] || sizes.md;

    return (
      <button
        ref={ref}
        className={`${base}${variantClass} ${sizeClass}${className}`}
        {...props}
      >
        {children}
      </button>
    );
  }
);

Button.displayName = "Button";
export default Button;
