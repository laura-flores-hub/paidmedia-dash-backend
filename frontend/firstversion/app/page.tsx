import {
  Users,
  Target,
  DollarSign,
  TrendingUp,
  MousePointerClick,
  Eye,
  Percent,
  Award,
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
import { leadData, campaignData } from "@/lib/dashboard-data";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function DashboardPage() {
  return (
    <div className="flex min-h-screen bg-background">
      {/* Sidebar */}
      <Sidebar />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0">
      {/* Header */}
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
            <div className="flex items-center gap-3">
              <Select defaultValue="30d">
                <SelectTrigger className="w-[140px] h-9 text-sm bg-secondary border-border/50">
                  <SelectValue placeholder="Select period" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="7d">Last 7 days</SelectItem>
                  <SelectItem value="30d">Last 30 days</SelectItem>
                  <SelectItem value="90d">Last 90 days</SelectItem>
                  <SelectItem value="ytd">Year to date</SelectItem>
                </SelectContent>
              </Select>
              <ThemeToggle />
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 overflow-auto px-4 py-6 sm:px-6 lg:px-8">
        {/* Lead KPIs */}
        <section className="mb-6">
          <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-3">
            Lead Metrics
          </h2>
          <div className="grid gap-4 grid-cols-2 lg:grid-cols-4">
            <KPICard
              title="Total Leads"
              value={leadData.total.toLocaleString()}
              subtitle="All time"
              trend={12}
              trendLabel="vs last period"
              icon={<Users className="h-4 w-4" />}
            />
            <KPICard
              title="This Month"
              value={leadData.thisMonth}
              subtitle="New leads"
              trend={8}
              trendLabel="vs last month"
              icon={<TrendingUp className="h-4 w-4" />}
            />
            <KPICard
              title="Qualified"
              value={leadData.qualified}
              subtitle={`${((leadData.qualified / leadData.thisMonth) * 100).toFixed(0)}% of total`}
              trend={15}
              trendLabel="vs last month"
              icon={<Target className="h-4 w-4" />}
            />
            <KPICard
              title="Avg. Lead Score"
              value={leadData.avgLeadScore}
              subtitle="Quality indicator"
              trend={5}
              trendLabel="vs last month"
              icon={<Award className="h-4 w-4" />}
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
              value={`$${(campaignData.totalSpend / 1000).toFixed(1)}k`}
              subtitle="This period"
              trend={-3}
              trendLabel="vs last period"
              icon={<DollarSign className="h-4 w-4" />}
            />
            <KPICard
              title="ROAS"
              value={`${campaignData.roas}x`}
              subtitle="Return on ad spend"
              trend={11}
              trendLabel="vs last period"
              icon={<Percent className="h-4 w-4" />}
            />
            <KPICard
              title="Cost per Lead"
              value={`$${campaignData.cpl.toFixed(2)}`}
              subtitle="Avg. acquisition cost"
              trend={-8}
              trendLabel="vs last period"
              icon={<MousePointerClick className="h-4 w-4" />}
            />
            <KPICard
              title="Click-Through Rate"
              value={`${campaignData.ctr}%`}
              subtitle={`${(campaignData.impressions / 1000000).toFixed(1)}M impressions`}
              trend={4}
              trendLabel="vs last period"
              icon={<Eye className="h-4 w-4" />}
            />
          </div>
        </section>

        {/* Charts Row */}
        <section className="mb-6 grid gap-4 lg:grid-cols-2">
          <LeadVolumeChart />
          <CampaignPerformanceChart />
        </section>

        {/* Middle Row: Sources + Insights */}
        <section className="mb-6 grid gap-4 lg:grid-cols-3">
          <LeadSourcesChart />
          <div className="lg:col-span-2">
            <InsightsPanel />
          </div>
        </section>

        {/* Tables Row */}
        <section className="mb-6 grid gap-4 lg:grid-cols-2">
          <CampaignsTable />
          <RecentLeadsTable />
        </section>
      </main>
      </div>
    </div>
  );
}
