"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { insights } from "@/lib/dashboard-data";
import { cn } from "@/lib/utils";
import { TrendingUp, AlertTriangle, Info, CheckCircle2 } from "lucide-react";

const getInsightIcon = (type: string) => {
  switch (type) {
    case "positive":
      return <CheckCircle2 className="h-4 w-4 text-chart-2" />;
    case "warning":
      return <AlertTriangle className="h-4 w-4 text-chart-3" />;
    case "negative":
      return <TrendingUp className="h-4 w-4 text-chart-4" />;
    default:
      return <Info className="h-4 w-4 text-chart-1" />;
  }
};

const getInsightBorder = (type: string) => {
  switch (type) {
    case "positive":
      return "border-l-chart-2";
    case "warning":
      return "border-l-chart-3";
    case "negative":
      return "border-l-chart-4";
    default:
      return "border-l-chart-1";
  }
};

export function InsightsPanel() {
  return (
    <Card className="border-border/50">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Key Insights</CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="space-y-3">
          {insights.map((insight, index) => (
            <div
              key={index}
              className={cn(
                "rounded-md border border-l-2 bg-secondary/50 p-3",
                getInsightBorder(insight.type)
              )}
            >
              <div className="flex items-start gap-2">
                {getInsightIcon(insight.type)}
                <div>
                  <p className="text-sm font-medium">{insight.title}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {insight.description}
                  </p>
                </div>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
