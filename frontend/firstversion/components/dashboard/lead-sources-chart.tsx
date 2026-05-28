"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { leadData } from "@/lib/dashboard-data";

export function LeadSourcesChart() {
  const maxLeads = Math.max(...leadData.sourceBreakdown.map((s) => s.leads));

  return (
    <Card className="border-border/50">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Lead Sources</CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="space-y-3">
          {leadData.sourceBreakdown.map((source, index) => (
            <div key={source.source} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span className="text-foreground">{source.source}</span>
                <span className="text-muted-foreground">
                  {source.leads} ({source.percentage}%)
                </span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-secondary">
                <div
                  className="h-1.5 rounded-full transition-all duration-500"
                  style={{
                    width: `${(source.leads / maxLeads) * 100}%`,
                    backgroundColor: `var(--color-chart-${(index % 5) + 1})`,
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
