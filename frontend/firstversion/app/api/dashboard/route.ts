import { NextResponse } from 'next/server'
import { createClient } from '@supabase/supabase-js'

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
)

function getDateRange(period: string, customStart?: string | null, customEnd?: string | null) {
  const now = new Date()
  const endDate = now.toISOString().split('T')[0]
  let startDate: string

  switch (period) {
    case '7d': {
      const d = new Date(now); d.setDate(d.getDate() - 7)
      startDate = d.toISOString().split('T')[0]
      break
    }
    case '90d': {
      const d = new Date(now); d.setDate(d.getDate() - 90)
      startDate = d.toISOString().split('T')[0]
      break
    }
    case 'ytd':
      startDate = `${now.getFullYear()}-01-01`
      break
    case 'custom':
      return { startDate: customStart || endDate, endDate: customEnd || endDate }
    default: {
      const d = new Date(now); d.setDate(d.getDate() - 30)
      startDate = d.toISOString().split('T')[0]
    }
  }
  return { startDate, endDate }
}

function mapSource(source: string): string {
  const map: Record<string, string> = {
    PAID_SEARCH: 'Google Ads',
    PAID_SOCIAL: 'Meta Ads',
    ORGANIC_SEARCH: 'Organic Search',
    SOCIAL_MEDIA: 'Social Media',
    EMAIL_MARKETING: 'Email',
    DIRECT_TRAFFIC: 'Direct',
    REFERRALS: 'Referral',
    OTHER_CAMPAIGNS: 'Other Paid',
  }
  return map[source] || source || 'Direct'
}

function isQualified(c: { lifecyclestage?: string; hs_lead_status?: string; conversion_de_lead?: string }) {
  return (
    ['salesqualifiedlead', 'opportunity', 'customer'].includes(c.lifecyclestage || '') ||
    c.hs_lead_status === 'qualified' ||
    c.conversion_de_lead === 'qualified'
  )
}

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const period = searchParams.get('period') || '30d'
    const { startDate, endDate } = getDateRange(
      period,
      searchParams.get('start'),
      searchParams.get('end')
    )

    const [metaRes, googleRes, linkedinRes, contactsRes, dealsRes] = await Promise.all([
      supabase
        .from('teste_data_meta_01')
        .select('campaign_name, cost, date_start')
        .gte('date_start', startDate)
        .lte('date_start', endDate),
      supabase
        .from('teste_data_google_01')
        .select('campaign_name, spend, date')
        .gte('date', startDate)
        .lte('date', endDate),
      supabase
        .from('teste_data_linkedin_01')
        .select('campaign_name, cost, date_start')
        .gte('date_start', startDate)
        .lte('date_start', endDate),
      supabase
        .from('teste_01')
        .select('hs_object_id, createdate, hs_analytics_source, hs_analytics_last_touch_converting_campaign, lifecyclestage, hs_lead_status, firstname, lastname, email, company, conversion_de_lead')
        .gte('createdate', `${startDate}T00:00:00`)
        .lte('createdate', `${endDate}T23:59:59`)
        .order('createdate', { ascending: false })
        .limit(5000),
      supabase
        .from('teste_data_deals_01')
        .select('amount, contact_ids, createdate')
        .gte('createdate', `${startDate}T00:00:00`)
        .lte('createdate', `${endDate}T23:59:59`)
        .not('amount', 'is', null)
        .gt('amount', 0),
    ])

    const meta = metaRes.data || []
    const google = googleRes.data || []
    const linkedin = linkedinRes.data || []
    const contacts = contactsRes.data || []
    const deals = dealsRes.data || []

    // --- Spend aggregation by campaign ---
    const spendMap: Record<string, { platform: string; spend: number }> = {}
    for (const r of meta) {
      const k = r.campaign_name || 'Unknown'
      spendMap[k] ??= { platform: 'Meta Ads', spend: 0 }
      spendMap[k].spend += r.cost || 0
    }
    for (const r of google) {
      const k = r.campaign_name || 'Unknown'
      spendMap[k] ??= { platform: 'Google Ads', spend: 0 }
      spendMap[k].spend += r.spend || 0
    }
    for (const r of linkedin) {
      const k = r.campaign_name || 'Unknown'
      spendMap[k] ??= { platform: 'LinkedIn', spend: 0 }
      spendMap[k].spend += r.cost || 0
    }
    const totalSpend = Object.values(spendMap).reduce((s, c) => s + c.spend, 0)

    // --- Contact attribution map: hs_object_id -> last-touch campaign ---
    const contactCampaignMap: Record<string, string> = {}
    for (const c of contacts) {
      if (c.hs_object_id && c.hs_analytics_last_touch_converting_campaign) {
        contactCampaignMap[String(c.hs_object_id)] = c.hs_analytics_last_touch_converting_campaign
      }
    }

    // Best-effort match: campaign name from HubSpot -> campaign key in spendMap
    function matchCampaign(name: string): string {
      const lower = name.toLowerCase()
      return (
        Object.keys(spendMap).find(
          k => k.toLowerCase() === lower ||
               k.toLowerCase().includes(lower) ||
               lower.includes(k.toLowerCase())
        ) || name
      )
    }

    // --- Revenue attribution: deal -> contact_ids -> campaign ---
    const revMap: Record<string, number> = {}
    let totalRevenue = 0
    for (const deal of deals) {
      const amount = deal.amount || 0
      totalRevenue += amount
      const cids: string[] = deal.contact_ids || []
      let attributed = false
      for (const cid of cids) {
        const campaign = contactCampaignMap[String(cid)]
        if (campaign) {
          const key = matchCampaign(campaign)
          revMap[key] = (revMap[key] || 0) + amount
          attributed = true
          break
        }
      }
      if (!attributed) {
        revMap['Unattributed'] = (revMap['Unattributed'] || 0) + amount
      }
    }

    // --- Leads per campaign ---
    const leadsMap: Record<string, number> = {}
    for (const c of contacts) {
      const camp = c.hs_analytics_last_touch_converting_campaign
      if (camp) {
        const key = matchCampaign(camp)
        leadsMap[key] = (leadsMap[key] || 0) + 1
      }
    }

    // --- Campaign rows ---
    const campaigns = Object.entries(spendMap)
      .map(([name, { platform, spend }]) => {
        const revenue = revMap[name] || 0
        const leads = leadsMap[name] || 0
        const cpl = leads > 0 ? spend / leads : 0
        const roas = spend > 0 ? revenue / spend : 0
        return {
          name,
          platform,
          spend: Math.round(spend * 100) / 100,
          leads,
          cpl: Math.round(cpl * 100) / 100,
          conversions: 0,
          revenue: Math.round(revenue * 100) / 100,
          roas: Math.round(roas * 100) / 100,
          status: 'active',
        }
      })
      .sort((a, b) => b.spend - a.spend)

    // --- Lead KPIs ---
    const now = new Date()
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString()
    const thisMonth = contacts.filter(c => c.createdate >= monthStart).length
    const qualified = contacts.filter(isQualified).length
    const conversionRate = contacts.length > 0
      ? Math.round((qualified / contacts.length) * 1000) / 10
      : 0

    // --- Source breakdown ---
    const sourceCount: Record<string, number> = {}
    for (const c of contacts) {
      const src = mapSource(c.hs_analytics_source || 'DIRECT_TRAFFIC')
      sourceCount[src] = (sourceCount[src] || 0) + 1
    }
    const totalContacts = contacts.length || 1
    const sourceBreakdown = Object.entries(sourceCount)
      .map(([source, leads]) => ({
        source,
        leads,
        percentage: Math.round((leads / totalContacts) * 100),
      }))
      .sort((a, b) => b.leads - a.leads)

    // --- Weekly trend (last 4 calendar weeks in range) ---
    const weeklyMap: Record<string, { leads: number; qualified: number }> = {}
    for (const c of contacts) {
      if (!c.createdate) continue
      const d = new Date(c.createdate)
      const wn = Math.ceil(
        ((d.getTime() - new Date(d.getFullYear(), 0, 1).getTime()) / 86400000 + 1) / 7
      )
      const label = `W${wn}`
      weeklyMap[label] ??= { leads: 0, qualified: 0 }
      weeklyMap[label].leads++
      if (isQualified(c)) weeklyMap[label].qualified++
    }
    const weeklyTrend = Object.entries(weeklyMap)
      .sort(([a], [b]) => parseInt(a.slice(1)) - parseInt(b.slice(1)))
      .slice(-4)
      .map(([week, data]) => ({ week, ...data }))

    // --- Daily spend / leads / revenue (last 7 days for chart) ---
    const dailySpend: Record<string, number> = {}
    const dailyLeads: Record<string, number> = {}
    const dailyRevenue: Record<string, number> = {}

    for (const r of meta) {
      const d = r.date_start?.slice(0, 10)
      if (d) dailySpend[d] = (dailySpend[d] || 0) + (r.cost || 0)
    }
    for (const r of google) {
      const d = r.date?.slice(0, 10)
      if (d) dailySpend[d] = (dailySpend[d] || 0) + (r.spend || 0)
    }
    for (const r of linkedin) {
      const d = r.date_start?.slice(0, 10)
      if (d) dailySpend[d] = (dailySpend[d] || 0) + (r.cost || 0)
    }
    for (const c of contacts) {
      const d = c.createdate?.slice(0, 10)
      if (d) dailyLeads[d] = (dailyLeads[d] || 0) + 1
    }
    for (const deal of deals) {
      const d = deal.createdate?.slice(0, 10)
      if (d && deal.amount) dailyRevenue[d] = (dailyRevenue[d] || 0) + deal.amount
    }

    const last7 = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(now)
      d.setDate(d.getDate() - (6 - i))
      return d.toISOString().split('T')[0]
    })
    const dailyPerformance = last7.map(d => ({
      date: new Date(d + 'T12:00:00').toLocaleDateString('en', { weekday: 'short' }),
      spend: Math.round((dailySpend[d] || 0) * 100) / 100,
      leads: dailyLeads[d] || 0,
      revenue: Math.round((dailyRevenue[d] || 0) * 100) / 100,
    }))

    // --- Recent leads ---
    const recentLeads = contacts.slice(0, 10).map(c => ({
      id: String(c.hs_object_id),
      name: [c.firstname, c.lastname].filter(Boolean).join(' ') || '—',
      company: c.company || '—',
      email: c.email || '—',
      source: mapSource(c.hs_analytics_source || ''),
      status: c.hs_lead_status || c.lifecyclestage || 'new',
      date: c.createdate
        ? new Date(c.createdate).toLocaleDateString('en', { month: 'short', day: 'numeric' })
        : '—',
    }))

    const roas = totalSpend > 0 ? Math.round((totalRevenue / totalSpend) * 100) / 100 : 0
    const cpl = contacts.length > 0
      ? Math.round((totalSpend / contacts.length) * 100) / 100
      : 0

    return NextResponse.json({
      leadData: {
        total: contacts.length,
        thisMonth,
        qualified,
        conversionRate,
        weeklyTrend,
        sourceBreakdown,
      },
      campaignData: {
        totalSpend: Math.round(totalSpend * 100) / 100,
        totalRevenue: Math.round(totalRevenue * 100) / 100,
        roas,
        cpl,
        campaigns,
        dailyPerformance,
      },
      recentLeads,
      period: { startDate, endDate },
    })
  } catch (err) {
    console.error('Dashboard API error:', err)
    return NextResponse.json({ error: 'Failed to load dashboard data' }, { status: 500 })
  }
}
