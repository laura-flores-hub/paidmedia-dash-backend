"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Line,
  ComposedChart,
} from "recharts";
import { TrendingUp, TrendingDown, DollarSign, Users, MousePointerClick, Eye } from "lucide-react";

interface Campaign {
  id: string;
  name: string;
  objective: string;
  status: string;
  spend: number;
  budget: number;
  impressions: number;
  clicks: number;
  ctr: number;
  leads: number;
  cpl: number;
  conversions: number;
  revenue: number;
  roas: number;
  startDate: string;
  audience: string;
}

interface WeeklyData {
  week: string;
  spend: number;
  leads: number;
  revenue: number;
}

interface PlatformCardProps {
  platform: {
    name: string;
    color: string;
    totalSpend: number;
    totalRevenue: number;
    roas: number;
    impressions: number;
    clicks: number;
    ctr: number;
    leads: number;
    cpl: number;
    conversions: number;
    campaigns: Campaign[];
    weeklyPerformance: WeeklyData[];
  };
  icon: React.ReactNode;
}

export function PlatformCard({ platform, icon }: PlatformCardProps) {
  const formatCurrency = (value: number) => {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    }).format(value);
  };

  const formatNumber = (value: number) => {
    if (value >= 1000000) {
      return (value / 1000000).toFixed(1) + "M";
    }
    if (value >= 1000) {
      return (value / 1000).toFixed(1) + "K";
    }
    return value.toString();
  };

  const activeCampaigns = platform.campaigns.filter((c) => c.status === "active").length;
  const pausedCampaigns = platform.campaigns.filter((c) => c.status === "paused").length;

  return (
    <Card className="border-border/50">
      <CardHeader className="pb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-secondary p-2.5">{icon}</div>
            <div>
              <CardTitle className="text-lg">{platform.name}</CardTitle>
              <p className="text-sm text-muted-foreground">
                {activeCampaigns} active, {pausedCampaigns} paused
              </p>
            </div>
          </div>
          <div className="text-right">
            <p className="text-2xl font-semibold">{platform.roas.toFixed(2)}x</p>
            <p className="text-xs text-muted-foreground">ROAS</p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* KPI Summary Row */}
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <DollarSign className="h-3.5 w-3.5" />
              <span className="text-xs">Spend</span>
            </div>
            <p className="text-lg font-medium">{formatCurrency(platform.totalSpend)}</p>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Users className="h-3.5 w-3.5" />
              <span className="text-xs">Leads</span>
            </div>
            <p className="text-lg font-medium">{platform.leads}</p>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Eye className="h-3.5 w-3.5" />
              <span className="text-xs">Impressions</span>
            </div>
            <p className="text-lg font-medium">{formatNumber(platform.impressions)}</p>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <MousePointerClick className="h-3.5 w-3.5" />
              <span className="text-xs">CTR</span>
            </div>
            <p className="text-lg font-medium">{platform.ctr.toFixed(2)}%</p>
          </div>
        </div>

        {/* Weekly Performance Chart */}
        <div>
          <p className="mb-3 text-sm font-medium">Weekly Performance</p>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={platform.weeklyPerformance}>
                <XAxis
                  dataKey="week"
                  tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  yAxisId="left"
                  tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
                />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    borderRadius: "6px",
                    fontSize: "12px",
                  }}
                  formatter={(value: number, name: string) => [
                    name === "spend" || name === "revenue"
                      ? formatCurrency(value)
                      : value,
                    name.charAt(0).toUpperCase() + name.slice(1),
                  ]}
                />
                <Bar
                  yAxisId="left"
                  dataKey="spend"
                  fill="hsl(var(--primary))"
                  radius={[4, 4, 0, 0]}
                  opacity={0.8}
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="leads"
                  stroke="hsl(var(--accent))"
                  strokeWidth={2}
                  dot={{ fill: "hsl(var(--accent))", strokeWidth: 0, r: 3 }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Campaigns Table */}
        <div>
          <p className="mb-3 text-sm font-medium">Campaigns</p>
          <div className="rounded-md border border-border/50 overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="text-xs">Campaign</TableHead>
                  <TableHead className="text-xs text-right">Spend</TableHead>
                  <TableHead className="text-xs text-right">Leads</TableHead>
                  <TableHead className="text-xs text-right">CPL</TableHead>
                  <TableHead className="text-xs text-right">ROAS</TableHead>
                  <TableHead className="text-xs text-right">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {platform.campaigns.map((campaign) => (
                  <TableRow key={campaign.id} className="hover:bg-secondary/50">
                    <TableCell>
                      <div>
                        <p className="text-sm font-medium">{campaign.name}</p>
                        <p className="text-xs text-muted-foreground">{campaign.objective}</p>
                      </div>
                    </TableCell>
                    <TableCell className="text-right text-sm">
                      {formatCurrency(campaign.spend)}
                    </TableCell>
                    <TableCell className="text-right text-sm">{campaign.leads}</TableCell>
                    <TableCell className="text-right text-sm">
                      ${campaign.cpl.toFixed(2)}
                    </TableCell>
                    <TableCell className="text-right">
                      <span
                        className={cn(
                          "text-sm font-medium",
                          campaign.roas >= 3
                            ? "text-accent"
                            : campaign.roas >= 2
                            ? "text-foreground"
                            : "text-destructive"
                        )}
                      >
                        {campaign.roas.toFixed(2)}x
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <Badge
                        variant={campaign.status === "active" ? "default" : "secondary"}
                        className={cn(
                          "text-xs",
                          campaign.status === "active"
                            ? "bg-accent/20 text-accent hover:bg-accent/30"
                            : "bg-muted text-muted-foreground"
                        )}
                      >
                        {campaign.status}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
