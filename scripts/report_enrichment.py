"""Read-only v2 analytics and optional standalone family × sector dashboard."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.analytics import report_from_db


def write_dashboard(report, path):
    # Plotly is an analysis-only dependency, never added to the scraper image.
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    distributions=report['distributions']
    matrix=distributions['family_sector']
    families=sorted({row['job_family'] for row in matrix})
    sectors=sorted({row['employer_sector'] or 'Not stated' for row in matrix})
    counts={(row['job_family'],row['employer_sector'] or 'Not stated'):row['jobs'] for row in matrix}
    fig=make_subplots(rows=3,cols=1,subplot_titles=['Job family × employer sector','Tools and technologies','Certifications'],vertical_spacing=0.1)
    if matrix:
        fig.add_trace(go.Heatmap(x=sectors,y=families,z=[[counts.get((family,sector),0) for sector in sectors] for family in families],colorscale='Blues'),row=1,col=1)
    for index,name in ((2,'tools'),(3,'certifications')):
        rows=distributions[name][:20]
        fig.add_trace(go.Bar(x=[row['jobs'] for row in rows],y=[row['value'] for row in rows],orientation='h',name=name),row=index,col=1)
    fig.update_layout(height=1250,showlegend=False,title=f"RTJobs · last 30 days · {report['enriched_jobs']} enriched jobs · {report['coverage_percent']:.2f}% coverage")
    if not matrix:
        fig.add_annotation(text='No current-schema enrichment data yet',x=0.5,y=1,xref='paper',yref='paper',showarrow=False)
    fig.write_html(path,include_plotlyjs=True,full_html=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--html',type=Path)
    args=parser.parse_args()
    report=report_from_db(args.db)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if args.html:write_dashboard(report,args.html)
    print(f"{report['enriched_jobs']} enriched jobs; {report['coverage_percent']:.2f}% coverage. Database unchanged.")


if __name__=='__main__':main()
