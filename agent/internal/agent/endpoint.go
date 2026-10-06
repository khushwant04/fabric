package agent

import (
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var publicDNSLabel = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`)
var publicDNSSuffix = regexp.MustCompile(`^[a-z]{2,}$`)

// ValidateInferenceURL accepts only a public HTTPS base URL. This is a declaration
// of the ingress address, not a claim that DNS, certificate issuance, or health passed.
func ValidateInferenceURL(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.ForceQuery || strings.Contains(raw, "#") || (parsed.Path != "" && parsed.Path != "/") || parsed.RawPath != "" {
		return "", fmt.Errorf("inference-url must be a public HTTPS base URL without credentials, path, query, or fragment")
	}
	if strings.HasSuffix(parsed.Host, ":") {
		return "", fmt.Errorf("inference-url has an empty port")
	}
	if port := parsed.Port(); port != "" {
		number, err := strconv.Atoi(port)
		if err != nil || number < 1 || number > 65535 {
			return "", fmt.Errorf("inference-url port must be between 1 and 65535")
		}
	}
	host := strings.ToLower(parsed.Hostname())
	if host == "localhost" || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") || strings.HasSuffix(host, ".svc") || strings.HasSuffix(host, ".svc.cluster.local") {
		return "", fmt.Errorf("inference-url must name a public ingress host")
	}
	if ip := net.ParseIP(host); ip != nil {
		if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsUnspecified() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
			return "", fmt.Errorf("inference-url must not name a private address")
		}
	} else {
		labels := strings.Split(host, ".")
		if len(host) > 253 || len(labels) < 2 || !publicDNSSuffix.MatchString(labels[len(labels)-1]) {
			return "", fmt.Errorf("inference-url must name a public DNS hostname")
		}
		for _, label := range labels {
			if len(label) > 63 || !publicDNSLabel.MatchString(label) {
				return "", fmt.Errorf("inference-url contains an invalid DNS label")
			}
		}
	}
	return strings.TrimSuffix(parsed.String(), "/"), nil
}
