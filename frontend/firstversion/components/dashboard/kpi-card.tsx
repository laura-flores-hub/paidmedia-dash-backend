"use client";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";

interface KPICardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  trend?: number;
  trendLabel?: string;
  icon?: React.ReactNode;
  className?: string;
}

export function KPICard({
  title,
  value,
  subtitle,
  trend,
  trendLabel,
  icon,
  className,
}: KPICardProps) {
  const getTrendIcon = () => {
    if (!trend) return <Minus className="h-3 w-3" />;
    if (trend > 0) return <TrendingUp className="h-3 w-3" />;
    return <TrendingDown className="h-3 w-3" />;
  };

  const getTrendColor = () => {
    if (!trend) return "text-muted-foreground";
    if (trend > 0) return "text-accent";
    return "text-destructive";
  };

  return (
    <Card className={cn("border-border/50", className)}>
      <CardContent className="pt-6">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <p className="text-sm text-muted-foreground">{title}</p>
            <p className="text-2xl font-semibold tracking-tight">{value}</p>
            {subtitle && (
              <p className="text-xs text-muted-foreground">{subtitle}</p>
            )}
          </div>
          {icon && (
            <div className="rounded-md bg-secondary p-2 text-muted-foreground">
              {icon}
            </div>
          )}
        </div>
        {trend !== undefined && (
          <div className={cn("mt-3 flex items-center gap-1 text-xs", getTrendColor())}>
            {getTrendIcon()}
            <span>
              {trend > 0 ? "+" : ""}
              {trend}%
            </span>
            {trendLabel && (
              <span className="text-muted-foreground">{trendLabel}</span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
