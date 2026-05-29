"use client";

import { useState, useEffect } from "react";
import {
  Users,
  Target,
  DollarSign,
  TrendingUp,
  MousePointerClick,
  TrendingDown,
} from "lucide-react";
import { KPICard } from "@/components/dashboard/kpi-card";
import { LeadVolumeChart } from "@/components/dashboard/lead-volume-chart";
import { CampaignPerformanceChart } from "@/components/dashboard/campaign-performance-chart";
import { LeadSourcesChart } from "@/components/dashboard/lead-sources-chart";
import { CampaignsTable } from "@/components/dashboard/campaigns-table";
import { RecentLeadsTable } from "@/components/dashboard/recent-leads-table";
import { InsightsPanel } from "@/components/dashboard/insights-panel";
import { Sidebar } from "@/components/dashboard/sidebar";
import { ThemeToggle } from "@/components/theme-toggle";
import type { DashboardData } from "@/lib/dashboard-data";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const EMPTY: DashboardData = {
  leadData: {
    total: 0,
    thisMonth: 0,
    qualified: 0,
    conversionRate: 0,
    weeklyTrend: [],
    sourceBreakdown: [],
  },
  campaignData: {
    totalSpend: 0,
    totalRevenue: 0,
    roas: 0,
    cpl: 0,
    campaigns: [],
    dailyPerformance: [],
  },
  recentLeads: [],
}

export default function DashboardPage() {
  const [period, setPeriod] = useState("30d");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [data, setData] = useState<DashboardData>(EMPTY);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (period === "custom" && (!customStart || !customEnd)) return;

    const params = new URLSearchParams({ period });
    if (period === "custom") {
      params.set("start", customStart);
      params.set("end", customEnd);
    }

    setLoading(true);
    fetch(`/api/dashboard?${params}`)
      .then(r => r.json())
      .then(json => { if (!json.error) setData(json) })
      .finally(() => setLoading(false));
  }, [period, customStart, customEnd]);

  const { leadData, campaignData, recentLeads } = data;

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />

      <div className="flex-1 flex flex-col min-w-0">
        <header className="border-b border-border/50 bg-card/50">
          <div className="px-4 py-4 sm:px-6 lg:px-8">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h1 className="text-xl font-semibold tracking-tight">
                  Leads & Campaign Performance
                </h1>
                <p className="text-sm text-muted-foreground mt-1">
                  Overview of inbound leads and paid campaign results
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Select value={period} onValueChange={setPeriod}>
                  <SelectTrigger className="w-[140px] h-9 text-sm bg-secondary border-border/50">
                    <SelectValue placeholder="Select period" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="7d">Last 7 days</SelectItem>
                    <SelectItem value="30d">Last 30 days</SelectItem>
                    <SelectItem value="90d">Last 90 days</SelectItem>
                    <SelectItem value="ytd">Year to date</SelectItem>
                    <SelectItem value="custom">Custom range</SelectItem>
                  </SelectContent>
                </Select>
                {period === "custom" && (
                  <div className="flex items-center gap-2">
                    <input
                      type="date"
                      value={customStart}
                      onChange={e => setCustomStart(e.target.value)}
                      className="h-9 rounded-md border border-border/50 bg-secondary px-2 text-sm text-foreground"
                    />
                    <span className="text-sm text-muted-foreground">–</span>
                    <input
                      type="date"
                      value={customEnd}
                      onChange={e => setCustomEnd(e.target.value)}
                      className="h-9 rounded-md border border-border/50 bg-secondary px-2 text-sm text-foreground"
                    />
                  </div>
                )}
                <ThemeToggle />
              </div>
            </div>
          </div>
        </header>

        <main className={`flex-1 overflow-auto px-4 py-6 sm:px-6 lg:px-8 transition-opacity duration-200 ${loading ? "opacity-50" : "opacity-100"}`}>
          {/* Lead KPIs */}
          <section className="mb-6">
            <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-3">
              Lead Metrics
            </h2>
            <div className="grid gap-4 grid-cols-2 lg:grid-cols-4">
              <KPICard
                title="Total Leads"
                value={leadData.total.toLocaleString()}
                subtitle="In selected period"
                icon={<Users className="h-4 w-4" />}
              />
              <KPICard
                title="This Month"
                value={leadData.thisMonth}
                subtitle="New leads"
                icon={<TrendingUp className="h-4 w-4" />}
              />
              <KPICard
                title="Qualified"
                value={leadData.qualified}
                subtitle={
                  leadData.total > 0
                    ? `${((leadData.qualified / leadData.total) * 100).toFixed(0)}% of total`
                    : "—"
                }
                icon={<Target className="h-4 w-4" />}
              />
              <KPICard
                title="Conversion Rate"
                value={`${leadData.conversionRate}%`}
                subtitle="Lead to qualified"
                icon={<TrendingDown className="h-4 w-4" />}
              />
            </div>
          </section>

          {/* Campaign KPIs */}
          <section className="mb-6">
            <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-3">
              Campaign Performance
            </h2>
            <div className="grid gap-4 grid-cols-2 lg:grid-cols-4">
              <KPICard
                title="Total Spend"
                value={
                  campaignData.totalSpend >= 1000
                    ? `$${(campaignData.totalSpend / 1000).toFixed(1)}k`
                    : `$${campaignData.totalSpend.toFixed(0)}`
                }
                subtitle="This period"
                icon={<DollarSign className="h-4 w-4" />}
              />
              <KPICard
                title="Total Revenue"
                value={
                  campaignData.totalRevenue >= 1000
                    ? `$${(campaignData.totalRevenue / 1000).toFixed(1)}k`
                    : `$${campaignData.totalRevenue.toFixed(0)}`
                }
                subtitle="Attributed deals"
                icon={<TrendingUp className="h-4 w-4" />}
              />
              <KPICard
                title="ROAS"
                value={campaignData.roas > 0 ? `${campaignData.roas}x` : "—"}
                subtitle="Return on ad spend"
                icon={<TrendingUp className="h-4 w-4" />}
              />
              <KPICard
                title="Cost per Lead"
                value={campaignData.cpl > 0 ? `$${campaignData.cpl.toFixed(2)}` : "—"}
                subtitle="Avg. acquisition cost"
                icon={<MousePointerClick className="h-4 w-4" />}
              />
            </div>
          </section>

          {/* Charts Row */}
          <section className="mb-6 grid gap-4 lg:grid-cols-2">
            <LeadVolumeChart weeklyTrend={leadData.weeklyTrend} />
            <CampaignPerformanceChart dailyPerformance={campaignData.dailyPerformance} />
          </section>

          {/* Middle Row: Sources + Insights */}
          <section className="mb-6 grid gap-4 lg:grid-cols-3">
            <LeadSourcesChart sourceBreakdown={leadData.sourceBreakdown} />
            <div className="lg:col-span-2">
              <InsightsPanel />
            </div>
          </section>

          {/* Tables Row */}
          <section className="mb-6 grid gap-4 lg:grid-cols-2">
            <CampaignsTable campaigns={campaignData.campaigns} />
            <RecentLeadsTable leads={recentLeads} />
          </section>
        </main>
      </div>
    </div>
  );
}
