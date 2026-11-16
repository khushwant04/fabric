package agent

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/servingmetrics"
)

// MetricsReporter samples serving metrics and sends them through send until the context
// ends. servingmetrics.Reporter implements it.
type MetricsReporter interface {
	Run(ctx context.Context, send servingmetrics.Sender)
}

// startMetricsReporter runs the serving-metrics reporter on its own client, like the host
// reporter, so a slow upload never delays reconciliation.
func (a *Agent) startMetricsReporter(ctx context.Context) {
	if a.config.MetricsReporter == nil || a.credentials == nil {
		return
	}
	stampID := a.credentials.StampID
	client := controlplane.New(a.config.ControlPlaneURL, a.config.RequestTimeout)
	client.CredentialSource = a.currentAgentCredential
	go a.config.MetricsReporter.Run(ctx, func(ctx context.Context, report servingmetrics.Report) error {
		err := client.ReportServingMetrics(ctx, stampID, report)
		var apiErr *controlplane.APIError
		if errors.As(err, &apiErr) {
			switch apiErr.Status {
			case http.StatusNotFound, http.StatusMethodNotAllowed:
				return fmt.Errorf("%w: %v", servingmetrics.ErrNotAccepted, err)
			case http.StatusUnprocessableEntity, http.StatusRequestEntityTooLarge:
				return fmt.Errorf("%w: %v", servingmetrics.ErrRejected, err)
			}
		}
		return err
	})
}
